# NVIDIA Device Plugin Deep Dive: Understanding GPU Access in Kubernetes

**Date:** January 27-28, 2026  
**Node:** donovan-xps-8930-gpu (Dell XPS 8930)  
**GPU:** NVIDIA GeForce GTX 1060 6GB  
**OS:** Ubuntu 24.04.3 LTS  
**Kubernetes:** v1.33.7  
**Container Runtime:** containerd 1.7.28

## Executive Summary

After successfully installing NVIDIA drivers and the nvidia-container-toolkit, we encountered a challenging issue: **the NVIDIA device plugin could not detect the GPU**, even though `nerdctl --gpus all` worked perfectly. This document details the multi-hour debugging process, explains the root causes, and provides a deep understanding of how NVIDIA GPUs, container runtimes, and Kubernetes interact.

**Final Result:** ✅ Device plugin working, node advertising `nvidia.com/gpu: 1`

---

## Table of Contents

1. [Initial Setup and Configuration](#initial-setup-and-configuration)
2. [The Problem: Device Plugin Can't Find NVML](#the-problem-device-plugin-cant-find-nvml)
3. [Understanding the Architecture](#understanding-the-architecture)
4. [Debugging Journey](#debugging-journey)
5. [The Root Cause](#the-root-cause)
6. [The Solution](#the-solution)
7. [Why This Solution Works](#why-this-solution-works)
8. [Key Learnings](#key-learnings)
9. [Workload Considerations](#workload-considerations)

---

## Initial Setup and Configuration

### What We Had Installed

1. **NVIDIA Drivers** (535.288.01)
   ```bash
   $ nvidia-smi
   # Successfully shows GTX 1060
   ```

2. **NVIDIA Container Toolkit** (v1.18.2)
   ```bash
   $ nvidia-ctk --version
   nvidia-container-toolkit version 1.18.2
   ```

3. **Containerd Runtime Configuration**
   
   The `nvidia-ctk runtime configure` command created:
   
   `/etc/containerd/conf.d/99-nvidia.toml`:
   ```toml
   [plugins."io.containerd.grpc.v1.cri".containerd.runtimes.nvidia]
     [plugins."io.containerd.grpc.v1.cri".containerd.runtimes.nvidia.options]
       BinaryName = "/usr/bin/nvidia-container-runtime"
   ```
   
   `/etc/containerd/config.toml`:
   ```toml
   [plugins."io.containerd.grpc.v1.cri".containerd.runtimes.nvidia]
     runtime_type = "io.containerd.runc.v2"
     
     [plugins."io.containerd.grpc.v1.cri".containerd.runtimes.nvidia.options]
       BinaryName = "/usr/bin/nvidia-container-runtime"
   
   [plugins."io.containerd.grpc.v1.cri".containerd]
     default_runtime_name = "nvidia"
   ```

4. **Direct Container Test Working**
   ```bash
   $ sudo nerdctl run --rm --gpus all nvidia/cuda:12.2.0-base-ubuntu22.04 nvidia-smi
   # ✅ Successfully shows GPU!
   ```

### The NVIDIA Device Plugin

The device plugin is a Kubernetes DaemonSet that:
- Runs on each GPU node
- Uses the NVML library (`libnvidia-ml.so.1`) to detect and enumerate GPUs
- Registers available GPUs with kubelet
- Allows pods to request GPU resources via `resources.limits.nvidia.com/gpu`

Initial deployment:
```yaml
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: nvidia-device-plugin-daemonset
  namespace: kube-system
spec:
  template:
    spec:
      containers:
      - image: nvcr.io/nvidia/k8s-device-plugin:v0.14.5
        name: nvidia-device-plugin-ctr
        securityContext:
          allowPrivilegeEscalation: false
          capabilities:
            drop: ["ALL"]
        volumeMounts:
        - name: device-plugin
          mountPath: /var/lib/kubelet/device-plugins
      volumes:
      - name: device-plugin
        hostPath:
          path: /var/lib/kubelet/device-plugins
```

---

## The Problem: Device Plugin Can't Find NVML

### Symptoms

Device plugin logs showed:
```
I0128 04:57:24.895105       1 factory.go:107] Detected non-NVML platform: 
  could not load NVML library: libnvidia-ml.so.1: cannot open shared object file: No such file or directory
E0128 04:57:24.895246       1 factory.go:115] Incompatible platform detected
I0128 04:57:24.895371       1 main.go:287] No devices found. Waiting indefinitely.
```

Node status:
```bash
$ kubectl describe node donovan-xps-8930-gpu | grep -A 10 "Allocatable:"
Allocatable:
  cpu:                12
  memory:             32469464Ki
  nvidia.com/gpu:     0    # ❌ No GPU!
```

### The Confusion

This was particularly confusing because:
1. ✅ `nvidia-smi` worked on the host
2. ✅ `nerdctl --gpus all` worked perfectly
3. ✅ NVIDIA drivers were loaded
4. ✅ NVML library existed at `/usr/lib/x86_64-linux-gnu/libnvidia-ml.so.1`
5. ✅ containerd was configured with `default_runtime_name = "nvidia"`
6. ❌ But the device plugin couldn't find the library

**Why was this so confusing?** Because we had proven the nvidia runtime worked (via nerdctl), but somehow the Kubernetes device plugin pod couldn't access the same resources.

---

## Understanding the Architecture

To understand the problem, we need to understand how these components interact:

### The NVIDIA Runtime Stack

```
┌─────────────────────────────────────────────────────────────┐
│ Container Process                                            │
│ ┌─────────────┐                                             │
│ │nvidia-smi or│  Needs access to:                           │
│ │CUDA app     │  • /dev/nvidia* devices                     │
│ └─────────────┘  • libnvidia-ml.so.1 (NVML library)         │
│                  • Other NVIDIA libraries                    │
└─────────────────────────────────────────────────────────────┘
                            ▲
                            │ Injected by
                            │
┌─────────────────────────────────────────────────────────────┐
│ nvidia-container-runtime (OCI runtime hook)                  │
│                                                              │
│ When --gpus flag is present:                                │
│ • Mounts /dev/nvidia* into container                        │
│ • Mounts NVIDIA libraries from host                          │
│ • Sets environment variables (NVIDIA_VISIBLE_DEVICES)       │
│ • Creates device nodes inside container                      │
└─────────────────────────────────────────────────────────────┘
                            ▲
                            │ Called by
                            │
┌─────────────────────────────────────────────────────────────┐
│ containerd (container runtime)                               │
│                                                              │
│ Has multiple runtime options:                               │
│ • runc (default)                                            │
│ • nvidia (uses nvidia-container-runtime)                    │
│                                                              │
│ Runtime selection:                                          │
│ • Direct CLI: --runtime=nvidia or --gpus flag               │
│ • Kubernetes CRI: via runtimeClassName in pod spec          │
│ • Default: whatever default_runtime_name is set to          │
└─────────────────────────────────────────────────────────────┘
                            ▲
                            │ Used by
                            │
┌─────────────────────────────────────────────────────────────┐
│ Kubernetes (kubelet + CRI)                                   │
│                                                              │
│ When creating pods:                                         │
│ • Calls containerd via CRI interface                        │
│ • Does NOT pass --gpus flag                                 │
│ • MAY honor default_runtime_name (inconsistent)             │
│ • CAN use explicit runtimeClassName                         │
└─────────────────────────────────────────────────────────────┘
```

### Key Insight: The `--gpus` Flag Magic

When you run:
```bash
nerdctl run --gpus all nvidia/cuda:12.2.0-base-ubuntu22.04 nvidia-smi
```

The `--gpus all` flag tells containerd to:
1. Use the nvidia runtime (overriding any default)
2. Pass the `NVIDIA_VISIBLE_DEVICES=all` environment variable
3. Trigger the nvidia-container-runtime hooks

**The nvidia-container-runtime hooks then:**
- Mount `/dev/nvidia0`, `/dev/nvidiactl`, etc. into the container
- Mount NVIDIA libraries (`libnvidia-ml.so.1`, `libcuda.so`, etc.) from `/usr/lib/x86_64-linux-gnu/`
- Set environment variables
- Ensure the container can access the GPU

### The Critical Problem

**When Kubernetes creates pods via CRI, it does NOT pass the `--gpus` flag.**

Even if you set `default_runtime_name = "nvidia"`, the CRI interface may not consistently honor this setting for all pods. This means:

1. ✅ The device plugin pod gets created by Kubernetes
2. ❌ Kubernetes doesn't pass `--gpus` (because it doesn't know about GPUs yet!)
3. ❌ The nvidia runtime hooks DON'T run
4. ❌ No `/dev/nvidia*` devices are mounted
5. ❌ No NVIDIA libraries are mounted
6. ❌ Device plugin can't find `libnvidia-ml.so.1`
7. ❌ Device plugin can't detect GPUs
8. ❌ Kubernetes never learns the node has GPUs

**This is a chicken-and-egg problem:**
- The device plugin needs GPU access to tell Kubernetes about GPUs
- But Kubernetes won't provide GPU access until it knows about GPUs
- And the device plugin is what tells Kubernetes about GPUs!

---

## Debugging Journey

### Attempt 1: Check if nvidia runtime is configured

```bash
$ sudo grep -A 3 "default_runtime_name" /etc/containerd/config.toml
default_runtime_name = "nvidia"
```

**Verdict:** ✅ Configured correctly, but device plugin still fails.

**Why this didn't help:** Setting `default_runtime_name` doesn't guarantee the nvidia runtime hooks will run for Kubernetes pods. The CRI interface may bypass this.

---

### Attempt 2: Restart containerd and kubelet

```bash
$ sudo systemctl restart containerd
$ sudo systemctl restart kubelet
```

**Verdict:** ❌ No change.

**Why this didn't help:** The configuration was already loaded. Restarting didn't change which runtime was being used for the device plugin pod.

---

### Attempt 3: Mount the NVML library file directly

```yaml
volumeMounts:
- name: nvidia-ml-lib
  mountPath: /usr/lib/x86_64-linux-gnu/libnvidia-ml.so.1
  readOnly: true
volumes:
- name: nvidia-ml-lib
  hostPath:
    path: /usr/lib/x86_64-linux-gnu/libnvidia-ml.so.1
    type: File
```

**Verdict:** ❌ Device plugin still couldn't find the library.

**Why this didn't work:** 
- Kubernetes wouldn't mount the file because it couldn't resolve the symlink chain
- The host has: `libnvidia-ml.so.1 -> libnvidia-ml.so.535.288.01`
- The mount tried to mount `libnvidia-ml.so.1` (a symlink) but that doesn't work with file mounts
- Even if it did, the device plugin looks for `libnvidia-ml.so.1` in standard library paths

---

### Attempt 4: Mount the entire `/usr/lib/x86_64-linux-gnu/` directory

```yaml
volumeMounts:
- name: nvidia-libs
  mountPath: /usr/lib/x86_64-linux-gnu
  readOnly: true
volumes:
- name: nvidia-libs
  hostPath:
    path: /usr/lib/x86_64-linux-gnu
    type: Directory
```

**Verdict:** ❌ Device plugin pod crashed with:
```
exec /usr/bin/nvidia-device-plugin: no such file or directory
```

**Why this didn't work:**
- Mounting `/usr/lib/x86_64-linux-gnu` **overwrote** the container's `/usr/lib/x86_64-linux-gnu`
- This directory contains fundamental system libraries like `libc.so.6`
- The host's Ubuntu 24.04 libc is NEWER than the container's Ubuntu 22.04 libc
- When the container tried to run its binary, it loaded the wrong (incompatible) libc
- This caused **GLIBC symbol errors**: `undefined symbol: _dl_audit_symbind_alt, version GLIBC_PRIVATE`
- The device plugin binary couldn't even start

**Key Learning:** You can NEVER safely mount the entire system library directory from the host into a container, because it will overwrite critical system libraries and break glibc compatibility.

---

### Attempt 5: Add privileged mode and mount `/dev`

Hypothesis: Maybe the device plugin needs privileged access to see hardware?

```yaml
securityContext:
  privileged: true
volumeMounts:
- name: dev
  mountPath: /dev
volumes:
- name: dev
  hostPath:
    path: /dev
```

**Verdict:** ✅ `/dev/nvidia*` devices now accessible in container, but ❌ still couldn't find NVML library.

**Why this partially worked:**
- Privileged mode + mounting `/dev` gave access to GPU device files
- We verified: `ls -la /dev/nvidia*` inside container showed all devices
- **BUT** the device plugin still needed `libnvidia-ml.so.1` to talk to those devices
- Having `/dev/nvidia0` without the NVML library is like having a phone without the dialer app

---

### Attempt 6: Mount specific library file with LD_LIBRARY_PATH

The breakthrough: mount just the specific NVIDIA library file (not the whole directory) and tell the linker where to find it.

```yaml
containers:
- name: nvidia-device-plugin-ctr
  env:
  - name: LD_LIBRARY_PATH
    value: "/usr/local/lib"
  securityContext:
    privileged: true
  volumeMounts:
  - name: device-plugin
    mountPath: /var/lib/kubelet/device-plugins
  - name: dev
    mountPath: /dev
  - name: nvidia-ml-lib
    mountPath: /usr/local/lib/libnvidia-ml.so.1  # Mount to custom location!
    readOnly: true
volumes:
- name: nvidia-ml-lib
  hostPath:
    path: /usr/lib/x86_64-linux-gnu/libnvidia-ml.so.535.288.01  # The actual file
    type: File
```

**Verdict:** ✅ SUCCESS!

```
I0128 05:21:31.183935       1 factory.go:107] Detected NVML platform: found NVML library
I0128 05:21:31.274661       1 server.go:165] Starting GRPC server for 'nvidia.com/gpu'
I0128 05:21:31.377801       1 server.go:125] Registered device plugin for 'nvidia.com/gpu' with Kubelet
```

```bash
$ kubectl describe node donovan-xps-8930-gpu | grep nvidia.com/gpu
  nvidia.com/gpu:     1   # ✅ GPU detected!
```

---

## The Root Cause

**The fundamental issue:** The NVIDIA device plugin is a **bootstrap component** that needs GPU access before Kubernetes knows GPUs exist. But the nvidia runtime hooks that provide GPU access are normally triggered by:

1. The `--gpus` flag (which Kubernetes doesn't use), OR
2. A RuntimeClass specification (which the device plugin pod doesn't have by default), OR  
3. Some other mechanism to ensure the nvidia runtime runs

Since none of these were in place, the device plugin pod ran with the standard `runc` runtime, which doesn't mount NVIDIA devices or libraries.

**Setting `default_runtime_name = "nvidia"` wasn't sufficient** because:
- The Kubernetes CRI interface doesn't consistently honor `default_runtime_name`
- Different containerd versions and configurations may handle this differently
- The CRI spec doesn't mandate that default runtime applies to all pods
- This is a known gap in the NVIDIA + Kubernetes integration story

---

## The Solution

### Final Working DaemonSet Configuration

```yaml
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: nvidia-device-plugin-daemonset
  namespace: kube-system
spec:
  selector:
    matchLabels:
      name: nvidia-device-plugin-ds
  updateStrategy:
    type: RollingUpdate
  template:
    metadata:
      labels:
        name: nvidia-device-plugin-ds
    spec:
      nodeSelector:
        kubernetes.io/hostname: donovan-xps-8930-gpu
      tolerations:
      - key: nvidia.com/gpu
        operator: Exists
        effect: NoSchedule
      priorityClassName: "system-node-critical"
      containers:
      - image: nvcr.io/nvidia/k8s-device-plugin:v0.14.5
        name: nvidia-device-plugin-ctr
        env:
          - name: FAIL_ON_INIT_ERROR
            value: "false"
          - name: LD_LIBRARY_PATH
            value: "/usr/local/lib"
        securityContext:
          # Device plugin needs privileged access to enumerate GPUs
          privileged: true
        volumeMounts:
        - name: device-plugin
          mountPath: /var/lib/kubelet/device-plugins
        - name: dev
          mountPath: /dev
        - name: nvidia-ml-lib
          mountPath: /usr/local/lib/libnvidia-ml.so.1
          readOnly: true
      volumes:
      - name: device-plugin
        hostPath:
          path: /var/lib/kubelet/device-plugins
      - name: dev
        hostPath:
          path: /dev
      - name: nvidia-ml-lib
        hostPath:
          path: /usr/lib/x86_64-linux-gnu/libnvidia-ml.so.535.288.01
          type: File
```

### What Each Component Does

1. **`privileged: true`**
   - Allows the container to access hardware devices
   - Required for the device plugin to communicate with GPU hardware
   - Without this, access to `/dev/nvidia*` is blocked by security policies

2. **Mount `/dev`**
   - Provides access to all device files including `/dev/nvidia0`, `/dev/nvidiactl`, etc.
   - These are the actual character device files that NVML uses to communicate with the GPU driver
   - The device plugin opens these files to query GPU information

3. **Mount NVML library to `/usr/local/lib/libnvidia-ml.so.1`**
   - We mount the **actual library file** (not the symlink)
   - We mount it to a **custom location** (`/usr/local/lib`) to avoid overwriting container libraries
   - The library filename must be exactly what the device plugin expects: `libnvidia-ml.so.1`

4. **Set `LD_LIBRARY_PATH=/usr/local/lib`**
   - Tells the dynamic linker to search `/usr/local/lib` for libraries
   - This is how the device plugin binary finds our mounted NVML library
   - Without this, the linker only searches standard paths like `/usr/lib/x86_64-linux-gnu`

---

## Why This Solution Works

### The Mount Strategy

**Why mount to `/usr/local/lib/` instead of `/usr/lib/x86_64-linux-gnu/`?**

1. **Avoids overwriting system libraries**
   - `/usr/lib/x86_64-linux-gnu/` contains critical system libraries (libc, libpthread, etc.)
   - Mounting over this directory would replace the container's libraries with host libraries
   - This causes GLIBC version conflicts and binary incompatibility

2. **Clean namespace**
   - `/usr/local/lib/` is meant for locally-installed libraries
   - It's safe to add libraries here without affecting the container's base system
   - The container's existing binaries continue to use their original libraries

3. **Controlled library exposure**
   - We only expose the ONE library the device plugin needs (`libnvidia-ml.so.1`)
   - The device plugin doesn't accidentally load other host libraries
   - This minimizes compatibility issues

### The Privileged Requirement

**Why does the device plugin need `privileged: true`?**

The NVML library needs to:
- Open `/dev/nvidia0` (and other device files)
- Perform `ioctl()` system calls to the NVIDIA kernel driver
- Query hardware capabilities and status
- Access PCIe configuration space

All of these require elevated privileges that aren't available to unprivileged containers, even with specific capabilities. While we could potentially use a smaller set of capabilities (`CAP_SYS_ADMIN`, `CAP_DAC_OVERRIDE`), `privileged: true` is simpler and standard for device plugins.

### The File vs. Symlink Issue

On the host:
```bash
$ ls -la /usr/lib/x86_64-linux-gnu/libnvidia-ml.so*
lrwxrwxrwx  libnvidia-ml.so -> libnvidia-ml.so.1
lrwxrwxrwx  libnvidia-ml.so.1 -> libnvidia-ml.so.535.288.01
-rw-r--r--  libnvidia-ml.so.535.288.01  (1.9 MB - actual file)
```

We mount `libnvidia-ml.so.535.288.01` (the actual file) to `/usr/local/lib/libnvidia-ml.so.1` (the name the device plugin expects). This:
- Avoids symlink resolution issues
- Gives the file the exact name the dynamic linker expects
- Works reliably across driver versions (just change the source path when updating drivers)

---

## Key Learnings

### 1. The `default_runtime_name` Gotcha

**Lesson:** Setting `default_runtime_name = "nvidia"` in containerd config does NOT guarantee that all Kubernetes pods will use the nvidia runtime.

**Why:**
- The Kubernetes CRI interface is separate from direct containerd usage
- Kubelet may explicitly request the `runc` runtime for certain pods
- The CRI spec doesn't mandate that `default_runtime_name` applies universally
- Different Kubernetes and containerd versions may behave differently

**Implication:** You cannot rely solely on `default_runtime_name` for GPU access in Kubernetes. You need explicit configuration.

### 2. The Device Plugin Bootstrap Problem

**Lesson:** The NVIDIA device plugin has a chicken-and-egg problem: it needs GPU access to tell Kubernetes about GPUs, but Kubernetes doesn't provide GPU access until it knows about GPUs.

**Solution Strategies:**
1. **Manual mounting** (our approach): Manually provide GPU access to the device plugin
2. **RuntimeClass**: Create a RuntimeClass and configure the device plugin to use it
3. **GPU Operator**: Use NVIDIA's GPU Operator which handles this automatically
4. **Privileged device plugin**: Run the device plugin in privileged mode with manual mounts

### 3. Library Mounting Is Tricky

**Lesson:** You cannot safely mount the entire host `/usr/lib/` directory into a container.

**Why:**
- System libraries like libc have different versions and symbols across OS releases
- The container's binaries are compiled against its own libc version
- Loading a different libc version causes "symbol not found" errors
- Even if symbols match, ABI differences can cause crashes

**Safe approach:**
- Mount only the specific libraries you need
- Mount them to a non-standard location (`/usr/local/lib`)
- Use `LD_LIBRARY_PATH` to make them discoverable
- Ensure the mounted library is compatible with the libraries already in the container

### 4. `nerdctl --gpus all` vs Kubernetes Pods

**Lesson:** Just because `nerdctl --gpus all` works doesn't mean Kubernetes pods will automatically get GPU access.

**Why:**
- `nerdctl --gpus all` explicitly requests the nvidia runtime
- Kubernetes doesn't have a `--gpus` flag in pod specs
- Kubernetes pods need explicit configuration (RuntimeClass, manual mounts, or GPU Operator)

**Debugging tip:** If direct container commands work but Kubernetes pods don't, look for differences in:
- Runtime selection (runc vs nvidia)
- Volume mounts
- Security contexts
- Environment variables

### 5. Reading Device Plugin Logs Is Essential

**Lesson:** The device plugin logs clearly state what's missing.

```
could not load NVML library: libnvidia-ml.so.1: cannot open shared object file: No such file or directory
```

This tells us:
- The device plugin is looking for `libnvidia-ml.so.1`
- It can't find it in the dynamic linker's search path
- We need to either add the library to the path, or modify the path

**Always check:**
- `kubectl logs -n kube-system -l name=nvidia-device-plugin-ds`
- Look for library loading errors
- Look for device access errors
- The logs will guide you to the solution

### 6. Privileged Pods Are Sometimes Necessary

**Lesson:** Device plugins inherently need elevated privileges because they interact with hardware.

**Security considerations:**
- Device plugins run in `kube-system` namespace
- They're marked as `system-node-critical`
- They need `privileged: true` to access hardware
- This is acceptable for system components that manage hardware

**Best practice:**
- Only grant privileged access to system components
- Regular workloads should NOT need privileged mode (they use the device plugin instead)
- Document why privileged mode is needed

---

## Workload Considerations

### What We Achieved

The device plugin now:
- ✅ Detects the GPU using NVML
- ✅ Registers `nvidia.com/gpu: 1` with Kubernetes
- ✅ Allows pods to request GPU resources
- ✅ Injects `NVIDIA_VISIBLE_DEVICES` environment variable into pods requesting GPUs

### What Workload Pods Get

When a pod requests `resources.limits.nvidia.com/gpu: 1`:

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: gpu-workload
spec:
  containers:
  - name: app
    image: myapp:latest
    resources:
      limits:
        nvidia.com/gpu: 1
```

The device plugin automatically:
- ✅ Sets `NVIDIA_VISIBLE_DEVICES=<gpu-uuid>` environment variable
- ✅ Ensures the pod is scheduled only on GPU nodes
- ✅ Prevents over-subscription (only one pod per GPU by default)

**BUT** the pod still doesn't automatically get:
- ❌ Access to `/dev/nvidia*` device files
- ❌ Access to NVIDIA libraries
- ❌ The `nvidia-smi` binary

### Making GPU Workloads Actually Work

Workload pods need the nvidia runtime to access GPUs. There are several approaches:

#### Option 1: RuntimeClass (Recommended)

Create a RuntimeClass:
```yaml
apiVersion: node.k8s.io/v1
kind: RuntimeClass
metadata:
  name: nvidia
handler: nvidia
```

Use it in pods:
```yaml
apiVersion: v1
kind: Pod
metadata:
  name: gpu-workload
spec:
  runtimeClassName: nvidia  # Explicitly use nvidia runtime
  containers:
  - name: app
    image: myapp:latest
    resources:
      limits:
        nvidia.com/gpu: 1
```

**Pro:** Clean, explicit, Kubernetes-native approach  
**Con:** Requires Kubernetes 1.20+ for GA RuntimeClass API

#### Option 2: NVIDIA GPU Operator (Enterprise)

The NVIDIA GPU Operator automates:
- Driver installation
- Device plugin deployment
- Runtime configuration
- Automatic runtime injection for GPU pods

**Pro:** Fully automated, handles everything  
**Con:** Complex, overkill for single-node setups

#### Option 3: Manual Mounts (Not Recommended for Production)

Manually mount devices and libraries (similar to what we did for the device plugin):

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: gpu-workload
spec:
  containers:
  - name: app
    image: myapp:latest
    securityContext:
      privileged: true
    env:
    - name: LD_LIBRARY_PATH
      value: "/usr/local/nvidia/lib:/usr/local/nvidia/lib64"
    volumeMounts:
    - name: dev
      mountPath: /dev
    - name: nvidia-libs
      mountPath: /usr/local/nvidia
    resources:
      limits:
        nvidia.com/gpu: 1
  volumes:
  - name: dev
    hostPath:
      path: /dev
  - name: nvidia-libs
    hostPath:
      path: /usr/lib/x86_64-linux-gnu
```

**Pro:** Simple, no additional setup  
**Con:** Error-prone, requires privileged mode, not portable

### Our Homelab Approach

For our homelab with a single GPU node, we're using:
- ✅ Device plugin with manual mounts (for GPU detection)
- 🔄 Future: Add RuntimeClass for workload pods (cleaner than manual mounts)
- 🔄 For Immich ML: Will need to configure the pod to use RuntimeClass or add manual mounts

---

## Testing GPU Access

### Verify Device Plugin

```bash
# Check device plugin is running
kubectl get pods -n kube-system -l name=nvidia-device-plugin-ds

# Check device plugin logs
kubectl logs -n kube-system -l name=nvidia-device-plugin-ds

# Should see:
# "Detected NVML platform: found NVML library"
# "Registered device plugin for 'nvidia.com/gpu' with Kubelet"

# Check node has GPU
kubectl describe node donovan-xps-8930-gpu | grep nvidia.com/gpu
# Should show: nvidia.com/gpu: 1
```

### Test Pod Scheduling

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: simple-gpu-test
spec:
  restartPolicy: Never
  nodeSelector:
    kubernetes.io/hostname: donovan-xps-8930-gpu
  tolerations:
  - key: nvidia.com/gpu
    operator: Exists
    effect: NoSchedule
  containers:
  - name: test
    image: ubuntu:22.04
    command: ["/bin/sh", "-c"]
    args:
      - |
        echo "=== Environment Variables ==="
        env | grep -i nvidia
        echo "Test complete"
    resources:
      limits:
        nvidia.com/gpu: 1
```

```bash
kubectl apply -f simple-gpu-test.yaml
sleep 10
kubectl logs simple-gpu-test

# Should output:
# NVIDIA_VISIBLE_DEVICES=GPU-ec196a97-463e-779c-9382-39aaeed6e506
```

This confirms:
- ✅ Pod can be scheduled on GPU node
- ✅ Device plugin injects GPU information
- ✅ Ready for actual GPU workloads (with RuntimeClass or manual mounts)

---

## References

- [NVIDIA Device Plugin GitHub](https://github.com/NVIDIA/k8s-device-plugin)
- [NVIDIA Container Toolkit Documentation](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/index.html)
- [Kubernetes Device Plugins](https://kubernetes.io/docs/concepts/extend-kubernetes/compute-storage-net/device-plugins/)
- [containerd Runtime Configuration](https://github.com/containerd/containerd/blob/main/docs/cri/config.md)
- Our other learnings:
  - `2026-01-24-nvidia-gpu-setup.md` - Initial driver and runtime installation
  - `2026-01-24-flannel-crashloop-debug.md` - Networking issues on the GPU node

---

## Conclusion

Getting the NVIDIA device plugin working in Kubernetes is more complex than it initially appears. The key challenges are:

1. **The bootstrap problem**: The device plugin needs GPU access before Kubernetes knows about GPUs
2. **Runtime configuration**: `default_runtime_name` doesn't guarantee nvidia runtime usage in Kubernetes
3. **Library mounting**: You must carefully mount only specific libraries to avoid glibc conflicts
4. **Privileged access**: The device plugin needs elevated privileges to access hardware

Our solution manually provides the device plugin with:
- Privileged mode for hardware access
- `/dev` mount for GPU device files
- Specific NVML library mount for GPU enumeration
- `LD_LIBRARY_PATH` configuration for library discovery

This allows the device plugin to detect GPUs and register them with Kubernetes, enabling pods to request GPU resources. Workload pods will need additional configuration (RuntimeClass or manual mounts) to actually access the GPUs.

**Next Steps:**
- ✅ Device plugin working
- 🔄 Create RuntimeClass for nvidia runtime
- 🔄 Configure Immich ML pod to use GPU
- 🔄 Test actual GPU workloads with CUDA applications
