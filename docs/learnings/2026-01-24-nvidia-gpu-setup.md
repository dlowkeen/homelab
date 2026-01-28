# NVIDIA GPU Setup on Dell XPS 8930 (Ubuntu 24.04)

This document summarizes the steps taken to install NVIDIA drivers and CUDA, and to prepare the system for GPU workloads in containers and Kubernetes. It also explains issues encountered with container runtimes and how they were resolved.

---

## 1. Installing NVIDIA Drivers and CUDA

```
sudo apt install -y ubuntu-drivers-common

ubuntu-drivers devices

sudo apt install -y nvidia-driver-535

# Verify installation
nvidia-smi
+---------------------------------------------------------------------------------------+
| NVIDIA-SMI 535.288.01             Driver Version: 535.288.01   CUDA Version: 12.2     |
|-----------------------------------------+----------------------+----------------------+
| GPU  Name                 Persistence-M | Bus-Id        Disp.A | Volatile Uncorr. ECC |
| Fan  Temp   Perf          Pwr:Usage/Cap |         Memory-Usage | GPU-Util  Compute M. |
|                                         |                      |               MIG M. |
|=========================================+======================+======================|
|   0  NVIDIA GeForce GTX 1060 6GB    Off | 00000000:01:00.0 Off |                  N/A |
| 38%   25C    P8               5W / 120W |      2MiB /  6144MiB |      0%      Default |
|                                         |                      |                  N/A |
+-----------------------------------------+----------------------+----------------------+

+---------------------------------------------------------------------------------------+
| Processes:                                                                            |
|  GPU   GI   CI        PID   Type   Process name                            GPU Memory |
|        ID   ID                                                             Usage      |
|=======================================================================================|
|  No running processes found                                                           |
+---------------------------------------------------------------------------------------+
```

## 2. Installing the NVIDIA Container Toolkit (required for K8s)

Kubernetes does not use system CUDA - it uses containerzed CUDA via this toolkit. 

Add NVIDIA repo: 
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg

curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list

Then install: `sudo apt install -y nvidia-container-toolkit`

### Why These Drivers Are Necessary

- GPU Workloads in Kubernetes or Containers rely on NVIDIA kernel drivers to interact with the GPU hardware.
- CUDA provides the use-space libraries and APIs required by GPU-accelerated applications.
- Without these drivers:
    - nvidia-smi will fail.
    - Containers requesting GPUs will fail to start or will not detect the GPU.

## 3. Install containerd

```
sudo apt install -y ca-certificates curl gnupg lsb-release
sudo apt install -y containerd
sudo systemctl enable containerd
sudo systemctl start containerd
# Generate default containerd config
sudo mkdir -p /etc/containerd
sudo containerd config default | sudo tee /etc/containerd/config.toml
```

In this config `/etc/containerd/config.toml`, we have to specify specific runtime configurations to enable use of the GPU.

Need to set this:
```
[plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runc.options]
  SystemdCgroup = true
```
This is required for kubelet stability.

And add this under the `[plugins."io.containerd.grpc.v1.cri".containerd.runtimes]` block.
```
[plugins."io.containerd.grpc.v1.cri".containerd.runtimes.nvidia]
  runtime_type = "io.containerd.runc.v2"

[plugins."io.containerd.grpc.v1.cri".containerd.runtimes.nvidia.options]
  BinaryName = "/usr/bin/nvidia-container-runtime"

[plugins."io.containerd.grpc.v1.cri".containerd]
  default_runtime_name = "nvidia"

```

## 6. Install NVIDIA Container Toolkit
```
sudo apt install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=containerd

```

## 7. Ran into issues using containerd so switched to `nerdctl`

Problem: ctr ignores the CRI plugin section. It only sees runtime names registered under [plugins."io.containerd.grpc.v1.cri".containerd.runtimes] that are recognized by containerd for non-CRI namespaces

# Replace VERSION with latest stable, e.g., 1.3.1
VERSION="1.3.1"
curl -LO https://github.com/containerd/nerdctl/releases/download/v${VERSION}/nerdctl-${VERSION}-linux-amd64.tar.gz
sudo tar Cxzvf /usr/local/bin nerdctl-${VERSION}-linux-amd64.tar.gz

### Advantages of nerdctl over raw ctr:

- Automatically selects the NVIDIA runtime if configured in containerd.
- Correctly handles --gpus all.
- Integrates with CNI networking (or Flannel) for Kubernetes workloads.
- Simplifies running GPU workloads without manually specifying hooks or device paths.


## Additional Gotchas:

My GPU set up is for a consumer AutoCAD program. So I had to disable it from the desktop in order for this machine to be treated like a true GPU node. Switching to multi-user target (headless mode):
```
sudo systemctl set-default multi-user.target
sudo systemctl disable gdm3
sudo reboot
```
