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
   
   The `nvidia-ctk runtime configure --runtime=containerd` command created:
   
   `/etc/containerd/conf.d/99-nvidia.toml` (comprehensive config, key sections shown):
   ```toml
   version = 2
   
   [plugins]
     [plugins."io.containerd.grpc.v1.cri"]
       # ... many CRI plugin settings ...
       
       [plugins."io.containerd.grpc.v1.cri".containerd]
         # ... containerd settings ...
         
         [plugins."io.containerd.grpc.v1.cri".containerd.runtimes]
           
           [plugins."io.containerd.grpc.v1.cri".containerd.runtimes.nvidia]
             runtime_type = "io.containerd.runc.v2"
             sandbox_mode = "podsandbox"
             
             [plugins."io.containerd.grpc.v1.cri".containerd.runtimes.nvidia.options]
               BinaryName = "/usr/bin/nvidia-container-runtime"
               SystemdCgroup = true
   ```
   
   **Note:** The actual file is ~160 lines with full CRI plugin configuration. The key parts are:
   - Defines the `nvidia` runtime under `runtimes.nvidia`
   - Sets `BinaryName = "/usr/bin/nvidia-container-runtime"`
   - Enables `SystemdCgroup = true` for kubelet compatibility
   
   We also manually edited `/etc/containerd/config.toml` to set the default runtime:
   ```toml
   [plugins."io.containerd.grpc.v1.cri".containerd]
     default_runtime_name = "nvidia"
   ```
   
   **Important:** The `nvidia-ctk runtime configure` command does NOT set `default_runtime_name`. 
   It only creates the runtime definition in `/etc/containerd/conf.d/99-nvidia.toml`. 
   Setting `default_runtime_name = "nvidia"` was a manual edit we made, but as we discovered, 
   this setting is not reliably honored by Kubernetes CRI.

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

## RuntimeClass vs GPU Operator: Detailed Comparison

Now that we understand the problem (device plugin works, but workloads need nvidia runtime), let's compare the two primary solutions for enabling GPU access in workload pods.

### Quick Summary

| Aspect | RuntimeClass | GPU Operator |
|--------|-------------|--------------|
| **Setup Time** | 5-10 minutes | 30-60 minutes |
| **Initial Complexity** | Low | Medium-High |
| **Ongoing Maintenance** | Minimal | Low-Medium |
| **Resource Overhead** | ~0 MB | ~500-1000 MB |
| **Components Added** | 1 (RuntimeClass object) | 10+ (Operator, controllers, DaemonSets) |
| **Best For** | Homelabs, small clusters | Multi-node production clusters |
| **Driver Management** | Manual (you handle it) | Automated (operator can install) |
| **Requires Pre-installed** | Drivers, toolkit, containerd config | Just Kubernetes |
| **Learning Curve** | Minimal | Moderate |
| **Troubleshooting** | Simple, transparent | Complex, layered |

---

### Option 1: RuntimeClass (Recommended for Homelabs)

#### What It Is

RuntimeClass is a Kubernetes-native resource that tells kubelet which container runtime to use for a pod. It's a simple pointer that says "use the nvidia runtime instead of runc for this pod."

**Conceptually:**
```
Pod spec says: "use RuntimeClass: nvidia"
    ↓
Kubernetes tells containerd: "use the nvidia runtime handler"
    ↓
containerd uses nvidia-container-runtime
    ↓
nvidia-container-runtime hooks inject GPU access
    ↓
Pod gets /dev/nvidia*, libraries, and nvidia-smi
```

#### Setup Steps

**Step 1: Verify nvidia runtime is configured** (we already have this)
```bash
# Check /etc/containerd/conf.d/99-nvidia.toml exists
sudo cat /etc/containerd/conf.d/99-nvidia.toml | grep -A 5 "runtimes.nvidia"
```

**Step 2: Create RuntimeClass** (one-time, 30 seconds)
```yaml
# runtime-class-nvidia.yaml
apiVersion: node.k8s.io/v1
kind: RuntimeClass
metadata:
  name: nvidia
handler: nvidia  # Must match the runtime name in containerd config
```

```bash
kubectl apply -f runtime-class-nvidia.yaml
```

**Step 3: Use it in pods**
```yaml
apiVersion: v1
kind: Pod
metadata:
  name: gpu-workload
spec:
  runtimeClassName: nvidia  # Just add this one line!
  containers:
  - name: app
    image: nvidia/cuda:12.2.0-base-ubuntu22.04
    command: ["nvidia-smi"]
    resources:
      limits:
        nvidia.com/gpu: 1
```

**That's it!** Three simple steps.

#### Pros

1. **Extremely Simple**
   - One YAML file with 5 lines
   - No new controllers or operators to manage
   - Native Kubernetes resource (stable since K8s 1.20)

2. **Zero Resource Overhead**
   - RuntimeClass is just metadata, no running components
   - No additional pods or controllers
   - No memory or CPU usage

3. **Transparent and Debuggable**
   - If something breaks, it's obvious: either the runtime isn't configured or the pod spec is wrong
   - No layers of abstraction to debug through
   - `kubectl describe pod` shows exactly what runtime was used

4. **Fine-Grained Control**
   - You choose which pods use the nvidia runtime
   - Other pods continue using standard runc runtime
   - No global changes to cluster behavior

5. **Works Immediately**
   - If your containerd config is correct, it works instantly
   - No waiting for operators to reconcile
   - No complex state management

#### Cons

1. **Manual Driver Management**
   - You must install NVIDIA drivers yourself (which you already did)
   - You must update drivers manually when needed
   - No automated driver lifecycle

2. **Manual Node Prep**
   - Each new GPU node needs manual driver installation
   - Each node needs containerd configuration
   - For 1-2 nodes this is fine, for 10+ nodes it's tedious

3. **No Monitoring Built-In**
   - RuntimeClass doesn't provide GPU metrics
   - You'd need to set up separate monitoring (DCGM exporter, etc.)
   - No dashboards or pre-configured alerts

4. **Pod-Level Configuration Required**
   - Every GPU pod needs `runtimeClassName: nvidia` in its spec
   - Easy to forget and have pods fail
   - Need to document for other users

5. **No Upgrade Automation**
   - When NVIDIA releases new drivers, you manually upgrade
   - When nvidia-container-toolkit updates, you manually upgrade
   - No rollback mechanisms if something breaks

#### Resource Usage

```
RuntimeClass resource: ~1 KB (just metadata)
Running components: 0
Memory overhead: 0 MB
CPU overhead: 0
```

#### Initial Setup Cost

- **Time:** 5-10 minutes
  - 2 minutes: Create RuntimeClass YAML
  - 1 minute: Apply to cluster
  - 2 minutes: Test with sample pod
  - 5 minutes: Update existing workload specs

- **Complexity:** Very Low
  - Prerequisites: Basic kubectl knowledge
  - Debugging: Simple (check pod spec, check containerd config)

- **Documentation needed:** 1 paragraph ("add runtimeClassName: nvidia to GPU pods")

#### When to Use RuntimeClass

✅ **Perfect for:**
- Homelabs (1-3 nodes)
- Learning/experimentation
- Small production clusters where you control all workloads
- When you want maximum transparency and control
- When resource efficiency matters

❌ **Not ideal for:**
- Large clusters (10+ nodes) where manual node prep is tedious
- Environments where non-admin users deploy GPU workloads (they need to remember runtimeClassName)
- When you want automated driver management

---

### Option 2: GPU Operator (Enterprise Standard)

#### What It Is

The NVIDIA GPU Operator is a Kubernetes operator that manages the entire GPU software stack as containers. Instead of installing drivers and tools on the host, it deploys them as DaemonSets that run on GPU nodes.

**Components it deploys:**
1. **nvidia-driver-daemonset** - Installs/manages NVIDIA drivers (optional)
2. **nvidia-container-toolkit-daemonset** - Installs nvidia-container-toolkit
3. **nvidia-device-plugin-daemonset** - Manages the device plugin (replaces our manual one)
4. **nvidia-dcgm-exporter** - Exports GPU metrics to Prometheus
5. **gpu-feature-discovery** - Labels nodes with GPU capabilities
6. **node-status-exporter** - Monitors node GPU health
7. **Operator controller** - Orchestrates all the above

**Conceptually:**
```
Deploy GPU Operator
    ↓
Operator detects GPU nodes
    ↓
Deploys driver DaemonSet (optional, can use host drivers)
    ↓
Deploys toolkit DaemonSet (configures containerd automatically)
    ↓
Deploys device plugin
    ↓
Configures RuntimeClass automatically
    ↓
Pods automatically use nvidia runtime (via admission webhook or RuntimeClass)
    ↓
Everything just works™
```

#### Setup Steps

**Step 1: Install the operator** (5-10 minutes)

```bash
# Add NVIDIA Helm repo
helm repo add nvidia https://helm.ngc.nvidia.com/nvidia
helm repo update

# Install GPU Operator
helm install --wait --generate-name \
  -n gpu-operator --create-namespace \
  nvidia/gpu-operator \
  --set driver.enabled=false  # Use host drivers (since you already installed them)
```

**Step 2: Wait for operator to deploy everything** (5-10 minutes)

The operator will automatically:
- Detect your GPU node
- Configure containerd
- Deploy device plugin
- Set up monitoring
- Create RuntimeClass

```bash
# Watch it deploy
kubectl get pods -n gpu-operator -w
```

**Step 3: Use GPU in pods** (automatic!)

With default config, pods requesting GPUs automatically get nvidia runtime. You might not even need `runtimeClassName`:

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: gpu-workload
spec:
  # runtimeClassName might not even be needed!
  containers:
  - name: app
    image: nvidia/cuda:12.2.0-base-ubuntu22.04
    command: ["nvidia-smi"]
    resources:
      limits:
        nvidia.com/gpu: 1
```

The operator's admission webhook can automatically inject the runtime class.

#### Pros

1. **Fully Automated**
   - Everything deployed as containers
   - Automatic configuration of all nodes
   - Self-healing (if components crash, they restart)

2. **Scales Well**
   - Add a new GPU node? Operator automatically configures it
   - 1 node or 100 nodes, same amount of work
   - No manual node preparation

3. **Built-in Monitoring**
   - DCGM exporter provides GPU metrics (utilization, temperature, power, memory)
   - Integrates with Prometheus/Grafana
   - Pre-built dashboards available

4. **Professional Support**
   - Official NVIDIA product
   - Regular updates and security patches
   - Documentation and community support

5. **Automated Upgrades**
   - Update the operator, it updates all components
   - Can manage driver upgrades (rolling updates)
   - Rollback support if issues occur

6. **Node Labeling**
   - Automatically labels nodes with GPU info (model, memory, CUDA version, etc.)
   - Makes scheduling smarter
   - Useful for heterogeneous GPU clusters

7. **User Friendly**
   - End users don't need to know about RuntimeClass
   - Just request `nvidia.com/gpu` and it works
   - Reduces cognitive load

#### Cons

1. **Significant Complexity**
   - 10+ new components to understand
   - Operator pattern adds abstraction layers
   - CRDs, controllers, webhooks, DaemonSets...

2. **Heavy Resource Usage**
   - Operator controller: ~200-300 MB RAM
   - Device plugin: ~50-100 MB RAM
   - DCGM exporter: ~100-200 MB RAM
   - GPU Feature Discovery: ~50 MB RAM
   - Node status exporter: ~50 MB RAM
   - **Total: ~500-1000 MB RAM overhead**

3. **Harder to Debug**
   - When something fails, need to debug operator logic
   - Multiple layers: Operator → DaemonSet → containerd → runtime
   - Logs spread across many pods

4. **Overkill for Small Deployments**
   - For 1-2 nodes, you're managing a complex operator just to avoid 10 minutes of manual work
   - Most features (auto-scaling, heterogeneous clusters) not needed

5. **Deployment Dependency**
   - Need Helm (or understand operator YAMLs)
   - Need to manage operator upgrades
   - Another component that can break

6. **Learning Curve**
   - Need to understand operators
   - Need to understand CRDs and custom resources
   - More concepts to learn

#### Resource Usage

```
Operator components: ~500-1000 MB RAM
Running pods: 6-10 DaemonSets + 1 Deployment (operator controller)
CPU overhead: ~0.5-1.0 cores (monitoring and controllers)
```

On your single GPU node:
- **Without GPU Operator:** 1 pod (device plugin) using ~50 MB
- **With GPU Operator:** 6-7 pods using ~600-800 MB

#### Initial Setup Cost

- **Time:** 30-60 minutes
  - 5 minutes: Understand operator options
  - 5 minutes: Install Helm chart
  - 10 minutes: Wait for everything to deploy
  - 10 minutes: Verify all components are healthy
  - 10 minutes: Test GPU workloads
  - 10 minutes: Understand how to troubleshoot when things go wrong

- **Complexity:** Medium-High
  - Prerequisites: Helm, understanding operators, debugging distributed systems
  - Debugging: Complex (operator logs, DaemonSet logs, webhook logs, containerd logs)

- **Documentation needed:** Multiple pages (operator config, troubleshooting, monitoring setup)

#### When to Use GPU Operator

✅ **Perfect for:**
- Large clusters (10+ GPU nodes)
- Production environments with SLAs
- Dynamic clusters (nodes added/removed frequently)
- Multi-tenant clusters (different users deploying GPU workloads)
- Heterogeneous GPU clusters (different GPU models)
- When you want automated driver management
- When you want built-in monitoring

❌ **Overkill for:**
- Single GPU node homelabs
- Learning/experimentation (hides important details)
- Resource-constrained environments
- When you want maximum transparency

---

### Side-by-Side Comparison

#### Scenario 1: Initial Setup

**RuntimeClass:**
```bash
# Create RuntimeClass (30 seconds)
cat <<EOF | kubectl apply -f -
apiVersion: node.k8s.io/v1
kind: RuntimeClass
metadata:
  name: nvidia
handler: nvidia
EOF

# Test it (1 minute)
kubectl run gpu-test --rm -it --image=nvidia/cuda:12.2.0-base-ubuntu22.04 \
  --overrides='{"spec":{"runtimeClassName":"nvidia","containers":[{"name":"gpu-test","image":"nvidia/cuda:12.2.0-base-ubuntu22.04","command":["nvidia-smi"],"resources":{"limits":{"nvidia.com/gpu":"1"}}}]}}'

# Total time: 2 minutes
# Lines of YAML: 5
# New components: 0
```

**GPU Operator:**
```bash
# Add Helm repo (2 minutes)
helm repo add nvidia https://helm.ngc.nvidia.com/nvidia
helm repo update

# Install operator (10 minutes - includes waiting for everything to deploy)
helm install --wait --generate-name \
  -n gpu-operator --create-namespace \
  nvidia/gpu-operator \
  --set driver.enabled=false

# Wait for all DaemonSets to be ready (5-10 minutes)
kubectl wait --for=condition=ready pod -l app=nvidia-device-plugin-daemonset -n gpu-operator --timeout=600s

# Test it (2 minutes)
kubectl run gpu-test --rm -it --image=nvidia/cuda:12.2.0-base-ubuntu22.04 \
  --overrides='{"spec":{"containers":[{"name":"gpu-test","image":"nvidia/cuda:12.2.0-base-ubuntu22.04","command":["nvidia-smi"],"resources":{"limits":{"nvidia.com/gpu":"1"}}}]}}'

# Total time: 15-25 minutes
# Lines of YAML: Hundreds (managed by Helm)
# New components: 6-10 pods
```

#### Scenario 2: Adding a Second GPU Node

**RuntimeClass:**
```bash
# On the new node (10 minutes of work):
# 1. Install NVIDIA drivers
sudo apt install -y nvidia-driver-535

# 2. Install nvidia-container-toolkit
sudo apt install -y nvidia-container-toolkit

# 3. Configure containerd
sudo nvidia-ctk runtime configure --runtime=containerd
sudo systemctl restart containerd

# 4. Join the node to the cluster
# (your normal node join process)

# 5. Label the node (if needed)
kubectl label node new-gpu-node accelerator=nvidia

# That's it! RuntimeClass automatically works on the new node.
```

**GPU Operator:**
```bash
# On the new node (0 minutes of work):
# Just join the node to the cluster with a GPU

# The operator detects the GPU and automatically:
# - Installs drivers (if enabled)
# - Configures containerd
# - Deploys device plugin
# - Sets up monitoring

# You do nothing. It just works.
```

**Winner for 2 nodes:** Tie (RuntimeClass is still fast for 2 nodes)  
**Winner for 10+ nodes:** GPU Operator (automation pays off)

#### Scenario 3: Troubleshooting "GPU not detected"

**RuntimeClass:**
```bash
# Debug steps are straightforward:

# 1. Is the nvidia runtime configured?
sudo cat /etc/containerd/conf.d/99-nvidia.toml | grep nvidia

# 2. Is the RuntimeClass being used?
kubectl describe pod my-gpu-pod | grep "Runtime Class"

# 3. Are drivers loaded?
nvidia-smi

# 4. Is containerd using the right runtime?
sudo journalctl -u containerd | grep nvidia

# That's it. 4 clear steps, clear failure points.
```

**GPU Operator:**
```bash
# Debug steps are layered:

# 1. Is the operator healthy?
kubectl get pods -n gpu-operator

# 2. Which component is failing?
kubectl logs -n gpu-operator -l app=nvidia-operator-validator

# 3. Are drivers installed? (if operator manages them)
kubectl logs -n gpu-operator -l app=nvidia-driver-daemonset

# 4. Is the device plugin running?
kubectl logs -n gpu-operator -l app=nvidia-device-plugin-daemonset

# 5. Is containerd configured?
# (Need to check on the node or via node-status-exporter)

# 6. Are there issues with the admission webhook?
kubectl logs -n gpu-operator -l app=gpu-operator

# 7. Is the RuntimeClass created?
kubectl get runtimeclass

# Many more moving parts to check.
```

**Winner:** RuntimeClass (simpler debugging)

#### Scenario 4: Monitoring GPU Usage

**RuntimeClass:**
```bash
# No built-in monitoring. You'd need to deploy your own:

# Option 1: Deploy DCGM exporter manually
kubectl apply -f https://raw.githubusercontent.com/NVIDIA/dcgm-exporter/main/deployment/dcgm-exporter.yaml

# Option 2: Use node-exporter with nvidia_gpu collector

# Then configure Prometheus to scrape it.
# No pre-built dashboards.
```

**GPU Operator:**
```bash
# DCGM exporter is included and configured!

# Metrics automatically available in Prometheus (if you have it):
# - GPU utilization
# - GPU memory usage
# - Temperature
# - Power consumption
# - More...

# Pre-built Grafana dashboards available:
# https://grafana.com/grafana/dashboards/12239
```

**Winner:** GPU Operator (monitoring included)

---

### Recommendation for Your Homelab

Given your cluster characteristics:
- **1 GPU node** (Dell XPS 8930)
- **Homelab environment** (learning-focused)
- **You already installed drivers and toolkit**
- **Resource-conscious**
- **Want to understand how things work**

**I recommend: RuntimeClass**

#### Why RuntimeClass for Your Setup

1. **Drivers Already Installed**
   - You already did the hard work (drivers, toolkit)
   - GPU Operator would just add unnecessary layers on top
   - You're not gaining the main benefit (automated driver management)

2. **Single Node**
   - The "automation for many nodes" benefit doesn't apply
   - 5 minutes to setup RuntimeClass vs 30 minutes for operator
   - Not worth the complexity for one node

3. **Learning Value**
   - You've already learned how the nvidia runtime works
   - RuntimeClass maintains that transparency
   - GPU Operator would hide what you just learned

4. **Resource Efficiency**
   - Your device plugin uses ~50 MB
   - GPU Operator would add ~500-800 MB overhead
   - That's significant in a homelab

5. **Simple to Maintain**
   - RuntimeClass has no ongoing maintenance
   - GPU Operator needs updates, monitoring, troubleshooting

#### When You'd Switch to GPU Operator

If you find yourself:
- Adding 3+ GPU nodes
- Wanting automated driver updates
- Needing GPU metrics/monitoring
- Running a multi-tenant cluster
- Wanting to stop managing node configuration

Then revisit GPU Operator. But for now, RuntimeClass is perfect.

---

### Implementation Guide: Setting Up RuntimeClass

Since I'm recommending RuntimeClass, here's exactly what to do:

**Step 1: Create RuntimeClass**

Create `infrastructure/nvidia-runtimeclass.yaml`:
```yaml
apiVersion: node.k8s.io/v1
kind: RuntimeClass
metadata:
  name: nvidia
handler: nvidia
```

Apply it:
```bash
kubectl apply -f infrastructure/nvidia-runtimeclass.yaml
```

Verify:
```bash
kubectl get runtimeclass nvidia
```

**Step 2: Update Your GPU Test Pods**

Modify `docs/learnings/gpu-test-pod.yaml` to use the RuntimeClass:

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: gpu-test-smi
spec:
  runtimeClassName: nvidia  # Add this line
  restartPolicy: Never
  nodeSelector:
    kubernetes.io/hostname: donovan-xps-8930-gpu
  tolerations:
  - key: nvidia.com/gpu
    operator: Exists
    effect: NoSchedule
  containers:
  - name: cuda-test
    image: nvidia/cuda:12.2.0-base-ubuntu22.04
    command: ["nvidia-smi"]
    resources:
      limits:
        nvidia.com/gpu: 1
```

**Step 3: Test It**

```bash
kubectl apply -f docs/learnings/gpu-test-pod.yaml
sleep 10
kubectl logs gpu-test-smi

# Should now show nvidia-smi output!
```

**Step 4: Update Immich ML**

When you want Immich ML to use the GPU, you'll need to:

1. Check if the Immich Helm chart supports `runtimeClassName` (many do)
2. Add it to your HelmRelease values:

```yaml
machine-learning:
  enabled: true
  runtimeClassName: nvidia  # Add this
  resources:
    limits:
      nvidia.com/gpu: 1
```

Or if the chart doesn't support it, use a kustomize patch in your overlay.

**That's it!** Your cluster is now fully GPU-enabled with minimal overhead and maximum transparency.

---

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

#### Option 2: NVIDIA GPU Operator (Enterprise Standard)

The NVIDIA GPU Operator automates:
- Driver installation (optional)
- Device plugin deployment
- Runtime configuration
- Automatic runtime injection for GPU pods
- Monitoring and metrics collection
- Upgrades and lifecycle management

**Pro:** Fully automated, handles everything, enterprise-grade, scales to many nodes  
**Con:** Complex, heavyweight (~500MB+ of components), overkill for single-node setups, requires understanding Kubernetes operators

**When to use GPU Operator:**
- ✅ **Multi-node GPU clusters** - Automates deployment across many nodes
- ✅ **Production environments** - Handles upgrades, monitoring, and lifecycle
- ✅ **Dynamic clusters** - Automatically configures new GPU nodes as they're added
- ✅ **Enterprise/team environments** - Standardized, supported approach
- ✅ **Mixed GPU types** - Handles different GPU models/drivers across nodes
- ❌ **Single GPU node homelab** - Overkill, manual setup is simpler and more educational
- ❌ **Learning/experimentation** - GPU Operator hides implementation details you might want to understand

**Why we didn't use it:**
- Our homelab has a single GPU node
- We wanted to understand the underlying mechanics
- Manual setup is more transparent and easier to troubleshoot
- Avoids the complexity of managing an operator
- Lower resource overhead (device plugin only vs full operator stack)

**If you're running a production cluster with multiple GPU nodes**, the GPU Operator is absolutely the recommended approach. It's what most enterprises use, and it's well-supported by NVIDIA.

**For homelabs and learning environments**, manual setup (what we did) is perfectly valid and arguably better for understanding how everything works.

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
- ✅ **Device plugin with manual mounts** (for GPU detection) - This is what we documented
- 🔄 **Future: Add RuntimeClass** for workload pods (cleaner than manual mounts per-pod)
- 🔄 **For Immich ML**: Will need to configure the pod to use RuntimeClass or add manual mounts

**Why this approach over GPU Operator?**
1. **Simplicity** - One DaemonSet vs entire operator framework
2. **Transparency** - We understand exactly what's happening
3. **Resource efficiency** - Minimal overhead for single node
4. **Educational** - Learned how NVML, runtimes, and device plugins work
5. **Sufficient** - Meets our needs without additional complexity

**Trade-off:** If we add more GPU nodes, we'd need to manually configure each one. At 3+ GPU nodes, the GPU Operator would likely be worth the complexity.

**Industry perspective:**
- **Small clusters (1-2 GPU nodes):** Manual setup is common and practical
- **Medium clusters (3-10 GPU nodes):** Mixed - some use GPU Operator, some use manual
- **Large clusters (10+ GPU nodes):** GPU Operator is standard
- **Cloud environments (GKE, EKS, AKS):** Cloud provider's managed GPU support (may use GPU Operator under the hood)

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
