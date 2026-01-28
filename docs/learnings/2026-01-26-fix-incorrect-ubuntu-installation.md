# Summary: Issues Encountered Setting Up the Old K8s Node on the NVMe Drive

## High-level problem
The Kubernetes node was unintentionally installed on the **16GB NVMe drive** instead of the intended **1TB Seagate SATA HDD**, due to a **loose SATA cable** that caused the Seagate drive to be undetected during installation.

---

## What went wrong

### 1. Incorrect disk detected during Ubuntu install
- During the initial Ubuntu installation, the system only detected a single disk.
- Assumption was that this disk was the **1TB Seagate HDD**, but in reality it was the **16GB NVMe drive**.
- Ubuntu (and later the K8s node setup) was installed entirely on the NVMe drive.

### 2. Root cause: flaky SATA cable
- The SATA data cable for the Seagate HDD had become partially disconnected.
- As a result:
  - The BIOS intermittently failed to detect the HDD.
  - The Ubuntu installer saw only the NVMe device and proceeded without warning.
- This was resolved by **physically reseating the cable and stabilizing it with electrical tape** to ensure a solid connection.

---

## Fix and recovery steps

### 3. Reinstall Ubuntu on the correct drive
- Once the Seagate HDD was consistently detected:
  - Ubuntu was reinstalled explicitly targeting `/dev/sda` (the Seagate drive).
- Partitioning was manually verified:
  - `/dev/sda1` → small **FAT32 EFI System Partition** mounted at `/boot/efi`
  - `/dev/sda2` → **ext4 root partition (`/`)**
- This ensured proper UEFI boot compatibility.

### 4. Bootloader and EFI issues
- After reinstall, the system continued to boot from the old NVMe Ubuntu install.
- The Seagate drive did not initially appear in BIOS boot priority.
- Resolution:
  - Verified EFI entries using `efibootmgr`.
  - Manually adjusted boot order so the Seagate-installed Ubuntu was preferred.
  - Confirmed correct boot by validating mounts with `lsblk`.

### 5. UEFI boot entry confusion
- BIOS showed generic entries like `Ubuntu` rather than disk-specific labels.
- This masked the fact that multiple Ubuntu installs existed.
- Using `efibootmgr` made it clear which EFI entry mapped to which disk — and allowed precise control over boot order.

---

## Kubernetes-specific cleanup

### 6. Old K8s node installed on the wrong disk
- The original node had joined the cluster from the NVMe-based OS.
- Plan moving forward:
  - Remove the old NVMe-backed node from the cluster.
  - Join the freshly installed Seagate-based node using a new `kubeadm join` command.

### 7. System prep corrections
- Swap was disabled permanently (required for kubelet).
- SSH server was reinstalled and enabled on the new OS.
- SSH keys had to be re-copied due to the host identity changing (same IP, new OS).

---

## Key takeaways / lessons learned

- Always verify detected disks in the installer (`lsblk`) before installing an OS.
- Loose SATA cables can fail *silently* and lead to misleading installation states.
- NVMe devices are often preferred by installers due to speed and detection order.
- UEFI boot entries are OS-based, not disk-based — `efibootmgr` is essential when multiple installs exist.
- Physically validating hardware connections can save hours of software debugging.
