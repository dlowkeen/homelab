# 2026-01-22 - Ubuntu Install + BIOS Recovery Summary (Dell XPS)

**Date:** 2026-01-22

This document summarizes the issues encountered and the fixes applied to successfully access BIOS, configure it correctly, and boot Ubuntu from a USB installer on a Dell XPS system (mid tower).

---

## 1. BIOS Reset Attempt 1

### Issue
- System would power on but **F2 and F12 would not open BIOS**
- Each time I tried to get to BIOS (Basic Input/Output System), the screen would just stay black with the fans humming. But if i let the machine boot up regularly, it would skip the Dell loading screen and go straight to the screen saver.
- This indicated that the machine was configured to "Fast Boot". This skips Power-On Self-Test (POST), an essential diagnostic sequence executed by a computer's BIOS/UEFI firmware immediately after powering on. POST ensures critical hardware components (cpu, ram, storage) are functional before loading the operating system. If errors are detected, the system alerts the user via beep codes or on-screen messages.
- The reason this machine skips POST is because the Dell XPS System is supposed to be suepr consumer friendly and 99.9% of consumers never need to access the BIOS settings.

## 2. CMOS / BIOS Reset Attempt 2

### Issue
- Since the BIOS settings are inaccessible through the F2 button, we needed to reset the BIOS settings byphysically removing the CMOS (Complementary Metal-Oxide Semiconductor) battery. The CMOS battery powers the CMOS chip on the motherboard which maintains essential settings like the system date, time, and BIOS hardware configurations. Even when the computer is powered down, the battery runs to ensure the system retains these configurations and the system starts up correctly.
- When I physically removed the battery, the CMOS chip would lose power and the BIOS configuration settings would be reset. 

### Fixes
- Physically removed the **CMOS battery** for ~5 minutes (this did not work initially)

## 3. CMOS / BIOS Reset Attempt 3

### Issue
- On this Dell XPS system, **removing the CMOS battery by itself was not sufficient** to fully reset the BIOS.
- Modern Dell systems store critical configuration data (including: BIOS passwords, Boot mode / Fast Boot state, Secure Boot settings) in **non-volatile memory (NVRAM)** that is *not* fully cleared just by removing the CMOS coin-cell battery.
- As a result, BIOS settings (including the state that prevented POST and BIOS access) persisted and the system continued to skip POST and block reliable BIOS entry.

### Fixes
- Dell provides **physical reset jumpers** on the motherboard that explicitly clear protected BIOS state.
- Using the jumpers forces the motherboard firmware to:
    - Clear BIOS passwords
    - Reset protected NVRAM regions
    - Reinitialize boot configuration
    - Restore factory-default BIOS behavior (including POST and splash screen display)
This is a **deeper reset** than a CMOS battery removal alone.

#### How the Jumper Reset Was Performed

1. Powered the system **completely off**
2. Unplugged the **power cable**
3. Removed the **CMOS battery**
4. Located the motherboard jumpers:
   - `P215` — Password Reset Jumper (2 pins with blue cap)
   - `CMOS` — CMOS Clear Jumper (2 pins, no cap)
   - `P217` — 3-pin jumper with cap on pins 1–2

5. Used a **flathead screwdriver** to briefly short the pins:
   - Touched the metal tip across both pins on:
     - `P215` (password reset)
     - `CMOS` jumper
   - Held contact for several seconds to discharge stored state

6. Put the CMOS battery back in
7. Reconnected power and booted the system

## 4. Unplugging All Boot Drives

As long as **any bootable operating system** was detected on a connected drive, the Dell XPS 8930 firmware followed an **aggressive fast-boot path**.

### Issue

When a valid OS was present:
- POST was minimized or skipped
- The Dell splash screen did not reliably appear
- Keyboard input (`F2`, `F12`) was often ignored
- The system immediately attempted to boot the existing OS

Even after clearing the BIOS using the CMOS battery and reset jumpers, this behavior persisted because the firmware still detected a bootable disk.

### Fix

By unplugging all SATA boot drives:
- The BIOS could no longer detect a valid operating system
- Fast-boot behavior was bypassed
- Full POST execution was forced
- The BIOS became fully interactive

With no bootable media available, the firmware fell back to its default behavior and displayed: `Checking Media Presence...`

This screen confirmed that:
- POST was running correctly
- The system was waiting for boot input
- BIOS setup access was now available

We were now finally able to access the BIOS set up.

## 5. BIOS Configuration Changes

### Issues
- USB boot blocked by legacy settings
- Ubuntu installer incompatible with default Dell RAID mode
- Secure Boot interfered with unsigned boot media

### Final BIOS Settings

| Setting | Value |
|------|------|
| Boot List Option | UEFI |
| Secure Boot | Disabled |
| Legacy Option ROMs | Disabled |
| Attempt Legacy Boot | Disabled |
| USB Boot Support | Enabled |
| SATA Operation | AHCI |

### Notes
- Changing SATA from **RAID On → AHCI** disables Intel Optane / RAID features but is required for Linux compatibility
- Secure Boot disabled to allow unsigned USB bootloader

---

## 6. Booting from USB

### Issues
- System booted to **“Checking Media Presence”** and Dell SupportAssist
- USB stick was not inserted during boot

### Fixes
- Powered system off
- Plugged in:
  - Ubuntu USB installer
  - Target installation drive
- Powered on and pressed `F12` once
- Selected USB device from boot menu

### Result
- Ubuntu live environment loaded successfully

---

## 7. Ubuntu Installation Choices

### Disk Layout (Automatic)
- `nvme0n1p1` → FAT32 → `/boot/efi`
- `nvme0n1p2` → ext4 → `/`

### Result
- Clean Ubuntu 24.04 LTS installation completed successfully

---

## 8. Post-Install Outcome

- System boots cleanly into Ubuntu
- SSH access configured for headless operation

