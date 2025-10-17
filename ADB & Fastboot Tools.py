#!/usr/bin/env python3
"""
ADB & Fastboot Tools by ItsFaa_
Merged full edition + Multi Flash (Batch) window

Requirements (recommended):
    pip install ttkbootstrap
    adb & fastboot in PATH
"""

import os
import sys
import shutil
import subprocess
import threading
import queue
import time
import zipfile
import tempfile
import datetime
import tkinter as tk
from tkinter import ttk, filedialog, simpledialog, messagebox

# try ttkbootstrap for nicer dark theme, fallback to ttk
try:
    import ttkbootstrap as tb
    from ttkbootstrap.constants import *
    BOOTSTRAP_AVAILABLE = True
except Exception:
    BOOTSTRAP_AVAILABLE = False

# ---------------- Config ----------------
ADB = shutil.which("adb") or "adb"
FASTBOOT = shutil.which("fastboot") or "fastboot"
DEVICE_POLL_INTERVAL = 1500  # ms
PRESET_DEBLOAT = [
    "com.miui.analytics",
    "com.xiaomi.account",
]
UNLOCK_COMMANDS = [
    [FASTBOOT, "flashing", "unlock"],
    [FASTBOOT, "oem", "unlock"],
    [FASTBOOT, "oem", "unlock-go"],
]
PARTITION_HINTS = {
    'boot': ['boot.img', 'boot.img.gz'],
    'recovery': ['recovery.img'],
    'system': ['system.img', 'system_new.img', 'system.raw.img', 'system_ext4.img'],
    'vbmeta': ['vbmeta.img'],
    'vendor': ['vendor.img'],
    'odm': ['odm.img'],
    'product': ['product.img'],
    'vendor_boot': ['vendor_boot.img'],
    'dtbo': ['dtbo.img'],
}
# ----------------------------------------

# Internal
output_q = queue.Queue()
proc_list_lock = threading.Lock()
current_procs = []  # list of subprocess.Popen objects being managed

def timestamp():
    return datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")

# ---------- Utilities ----------
def append_term(widget, txt):
    widget.configure(state="normal")
    widget.insert(tk.END, txt)
    widget.see(tk.END)
    widget.configure(state="disabled")

def is_bin_available(binname):
    return shutil.which(binname) is not None

def save_text_to_file(content, initial="log.txt"):
    fn = filedialog.asksaveasfilename(defaultextension=".txt", initialfile=initial, filetypes=[("Text files","*.txt")])
    if fn:
        with open(fn, "w", encoding="utf-8") as f:
            f.write(content)
        messagebox.showinfo("Saved", f"Saved to {fn}")

# ---------- Subprocess runner (thread-safe) ----------
def run_cmd_stream(cmd, term_widget, env=None, dry_run=False):
    """
    Run subprocess and stream output into output_q.
    cmd: list or string (list preferred)
    This function blocks until process finishes and streams output lines to output_q.
    """
    if isinstance(cmd, list):
        display = " ".join(cmd)
    else:
        display = cmd

    output_q.put(f"\n$ {display}\n")
    if dry_run:
        output_q.put("[DRY-RUN] Command not executed.\n")
        return 0

    try:
        if isinstance(cmd, list):
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
        else:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, shell=True)
    except FileNotFoundError:
        output_q.put(f"Error: binary not found: {cmd[0] if isinstance(cmd, list) else cmd.split()[0]}\n")
        return -1
    except Exception as e:
        output_q.put(f"Error starting command {display}: {e}\n")
        return -1

    with proc_list_lock:
        current_procs.append(proc)

    try:
        for line in proc.stdout:
            output_q.put(line)
    except Exception as e:
        output_q.put(f"Error reading subprocess output: {e}\n")
    finally:
        try:
            proc.wait()
            output_q.put(f"\n[Process exited with code {proc.returncode}]\n")
        except Exception as e:
            output_q.put(f"\n[Process wait error: {e}]\n")
        with proc_list_lock:
            try:
                current_procs.remove(proc)
            except ValueError:
                pass
    return getattr(proc, "returncode", -1)

def start_cmd(cmd, term_widget, dry_run=False):
    t = threading.Thread(target=run_cmd_stream, args=(cmd, term_widget, None, dry_run), daemon=True)
    t.start()
    return t

def stop_all_current():
    stopped_any = False
    with proc_list_lock:
        procs_copy = list(current_procs)
    for p in procs_copy:
        try:
            if p and p.poll() is None:
                p.terminate()
                time.sleep(0.2)
                if p.poll() is None:
                    p.kill()
                stopped_any = True
        except Exception:
            pass
    return stopped_any

# ---------- Device detection ----------
def adb_devices_list():
    try:
        res = subprocess.run([ADB, "devices", "-l"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=3)
        return res.stdout
    except Exception:
        return ""

def fastboot_devices_list():
    try:
        res = subprocess.run([FASTBOOT, "devices"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=3)
        return res.stdout
    except Exception:
        return ""

def detect_device_state():
    adb_out = adb_devices_list()
    if adb_out:
        lines = [ln for ln in adb_out.splitlines() if ln.strip()]
        for ln in lines:
            if not ln.lower().startswith("list of devices"):
                if "device" in ln.split():
                    return ("adb", ln.strip())
    fb_out = fastboot_devices_list()
    if fb_out and fb_out.strip():
        return ("fastboot", fb_out.strip())
    return ("none", "")

def fastboot_getvar_all():
    try:
        res = subprocess.run([FASTBOOT, "getvar", "all"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=6)
        combined = (res.stdout or "") + "\n" + (res.stderr or "")
        return combined
    except Exception as e:
        return f"Error getting getvar all: {e}"

# ---------- Unlock / Lock workers ----------
def unlock_worker(term_widget, dry_run=False, force=False, logfile=None):
    output_q.put("\n=== START UNIVERSAL UNLOCK ATTEMPT ===\n")
    if not is_bin_available(FASTBOOT):
        output_q.put("fastboot not found in PATH. Aborting.\n")
        return

    fb_list = fastboot_devices_list()
    if not fb_list.strip():
        output_q.put("No fastboot device detected. Put device in bootloader/fastboot mode.\n")
        return

    getvar = fastboot_getvar_all()
    output_q.put("[fastboot getvar all]\n")
    output_q.put(getvar + "\n")

    vendor_hint = ""
    lower = getvar.lower()
    if "xiaomi" in lower or "redmi" in lower:
        vendor_hint = "Xiaomi detected — may require Mi Unlock (account/token)."
    elif "samsung" in lower:
        vendor_hint = "Samsung detected — modern Samsung often locked; Odin/vendor tools likely needed."
    elif "huawei" in lower:
        vendor_hint = "Huawei detected — often requires official unlock code."
    elif "oneplus" in lower or "oppo" in lower or "realme" in lower:
        vendor_hint = "OnePlus/OPPO/Realme family — often support fastboot unlock but some models need token."
    if vendor_hint:
        output_q.put(f"[Vendor hint] {vendor_hint}\n")

    if not dry_run and not force:
        ans = simpledialog.askstring("Confirm Unlock",
                                     "Unlocking will likely ERASE DATA and void warranty.\n"
                                     "Type EXACTLY 'unlock' to proceed, or Cancel to abort.")
        if ans is None or ans.strip().lower() != "unlock":
            output_q.put("User cancelled unlock (confirmation not given).\n")
            output_q.put("=== END UNIVERSAL UNLOCK ATTEMPT ===\n")
            return

    lf = None
    if logfile:
        try:
            lf = open(logfile, "a", encoding="utf-8")
            lf.write("\n\n=== LOG START: " + timestamp() + " ===\n")
            lf.write("[fastboot getvar all]\n")
            lf.write(getvar + "\n")
            lf.flush()
        except Exception:
            lf = None

    for cmd in UNLOCK_COMMANDS:
        output_q.put(f"Trying: {' '.join(cmd)}\n")
        if lf:
            lf.write(f"\n$ {' '.join(cmd)}\n"); lf.flush()
        if dry_run:
            output_q.put("[DRY-RUN] skip execution\n")
            if lf: lf.write("[DRY-RUN] skip execution\n")
            continue

        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
        except FileNotFoundError:
            output_q.put(f"Error: binary not found: {cmd[0]}\n")
            if lf: lf.write(f"Error: binary not found: {cmd[0]}\n")
            continue
        except Exception as e:
            output_q.put(f"Error starting {cmd}: {e}\n")
            if lf: lf.write(f"Error starting {cmd}: {e}\n")
            continue

        with proc_list_lock:
            current_procs.append(proc)

        try:
            for line in proc.stdout:
                output_q.put(line)
                if lf: lf.write(line); lf.flush()
        except Exception as e:
            output_q.put(f"Error reading output: {e}\n")
            if lf: lf.write(f"Error reading output: {e}\n")
        finally:
            try:
                proc.wait()
                output_q.put(f"[Exited with {proc.returncode}]\n")
                if lf: lf.write(f"[Exited with {proc.returncode}]\n")
            except Exception as e:
                output_q.put(f"[Wait error: {e}]\n")
                if lf: lf.write(f"[Wait error: {e}]\n")
            with proc_list_lock:
                try:
                    current_procs.remove(proc)
                except ValueError:
                    pass

        if proc.returncode == 0:
            output_q.put("Command returned code 0 — likely success.\n")
            if lf: lf.write("Command return 0 — likely success.\n")
            break
        else:
            output_q.put("No success indication — trying next method.\n")
            if lf: lf.write("No success indication — trying next.\n")

    output_q.put("=== END UNIVERSAL UNLOCK ATTEMPT ===\n")
    if lf:
        lf.write("=== LOG END ===\n"); lf.close()

def lock_worker(term_widget, dry_run=False, force=False):
    output_q.put("\n=== START LOCK BOOTLOADER ATTEMPT ===\n")
    if not is_bin_available(FASTBOOT):
        output_q.put("fastboot not found in PATH. Aborting.\n")
        return
    fb_list = fastboot_devices_list()
    if not fb_list.strip():
        output_q.put("No fastboot device detected.\n")
        return

    preferred = [ [FASTBOOT, "flashing", "lock"], [FASTBOOT, "oem", "lock"] ]
    for cmd in preferred:
        output_q.put(f"Trying: {' '.join(cmd)}\n")
        if dry_run:
            output_q.put("[DRY-RUN] skip execution\n")
            continue
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
        except Exception as e:
            output_q.put(f"Error starting {cmd}: {e}\n")
            continue
        with proc_list_lock:
            current_procs.append(proc)
        try:
            for line in proc.stdout:
                output_q.put(line)
        except Exception as e:
            output_q.put(f"Error reading output: {e}\n")
        finally:
            try:
                proc.wait()
                output_q.put(f"[Exited with {proc.returncode}]\n")
            except Exception as e:
                output_q.put(f"[Wait error: {e}\n")
            with proc_list_lock:
                try:
                    current_procs.remove(proc)
                except ValueError:
                    pass
        if proc.returncode == 0:
            output_q.put("Bootloader lock command returned 0 — likely locked.\n")
            break
    output_q.put("=== END LOCK BOOTLOADER ATTEMPT ===\n")

# ---------- Auto-flash ZIP helpers ----------
def map_images_to_partitions(img_files):
    mapped = []
    lower_files = [(os.path.basename(p).lower(), p) for p in img_files]
    for part, hints in PARTITION_HINTS.items():
        for hint in hints:
            for name, full in lower_files:
                if hint in name:
                    mapped.append((part, full))
    if not mapped:
        for name, full in lower_files:
            if name.endswith('.img'):
                part_guess = name.rsplit('.img', 1)[0]
                mapped.append((part_guess, full))
    return mapped

def auto_flash_zip_worker(term_widget, zipfile_path, dry_run=False):
    output_q.put(f"\n=== START AUTO FLASH ZIP: {zipfile_path} ===\n")
    if not is_bin_available(FASTBOOT):
        output_q.put("fastboot not found in PATH. Aborting.\n")
        return
    if not os.path.isfile(zipfile_path):
        output_q.put("ZIP file not found.\n")
        return

    tmpdir = tempfile.mkdtemp(prefix="sdewa_flash_")
    try:
        try:
            with zipfile.ZipFile(zipfile_path, 'r') as z:
                z.extractall(tmpdir)
        except Exception as e:
            output_q.put(f"Failed to extract zip: {e}\n")
            return

        img_files = []
        for rootp, dirs, files in os.walk(tmpdir):
            for f in files:
                if f.lower().endswith('.img'):
                    img_files.append(os.path.join(rootp, f))
        if not img_files:
            output_q.put("No .img files found inside ZIP. Is this the correct firmware package?\n")
            return

        mapped = map_images_to_partitions(img_files)
        if not mapped:
            output_q.put("Could not map images to partitions automatically; will use filename heuristics.\n")
            for full in img_files:
                part = os.path.basename(full).split('.img')[0]
                mapped.append((part, full))

        output_q.put("Planned flash operations:\n")
        for p, f in mapped:
            output_q.put(f" - {p} -> {f}\n")

        if not dry_run:
            ok = messagebox.askyesno("Confirm Auto Flash", f"Will flash {len(mapped)} image(s) to device. Continue?")
            if not ok:
                output_q.put("User cancelled auto-flash.\n")
                return

        for p, f in mapped:
            cmd = [FASTBOOT, 'flash', p, f]
            output_q.put(f"Executing: {' '.join(cmd)}\n")
            if dry_run:
                output_q.put("[DRY-RUN] skip execution\n")
                continue
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
            except Exception as e:
                output_q.put(f"Error starting flash {p}: {e}\n")
                continue
            with proc_list_lock:
                current_procs.append(proc)
            try:
                for line in proc.stdout:
                    output_q.put(line)
            except Exception as e:
                output_q.put(f"Error reading output: {e}\n")
            finally:
                try:
                    proc.wait()
                    output_q.put(f"[Exited with {proc.returncode}]\n")
                except Exception as e:
                    output_q.put(f"[Wait error: {e}\n")
                with proc_list_lock:
                    try:
                        current_procs.remove(proc)
                    except ValueError:
                        pass

        output_q.put("=== END AUTO FLASH ZIP ===\n")
    finally:
        try:
            shutil.rmtree(tmpdir)
        except Exception:
            pass

# ---------- Auto-detect suggestion ----------
def detect_unlock_suggestion():
    try:
        res = subprocess.run([ADB, 'shell', 'getprop', 'ro.product.manufacturer'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=4)
        manuf = (res.stdout or '').strip().lower()
    except Exception:
        manuf = ''
    fb_all = fastboot_getvar_all().lower()

    if 'xiaomi' in manuf or 'xiaomi' in fb_all or 'redmi' in fb_all:
        return 'Xiaomi detected: may need Mi Unlock (official) or token; fastboot may not be enough.'
    if 'samsung' in manuf or 'samsung' in fb_all:
        return 'Samsung detected: many models locked; vendor tools (Odin) often required.'
    if 'huawei' in manuf or 'huawei' in fb_all:
        return 'Huawei detected: often requires official unlock code.'
    if 'oneplus' in manuf or 'oppo' in manuf or 'realme' in manuf or 'oneplus' in fb_all:
        return 'OnePlus/OPPO/Realme: usually supports fastboot oem unlock, but some require token.'
    if 'pixel' in manuf or 'google' in manuf or 'android' in fb_all:
        return 'Google/Pixel or generic Android: fastboot flashing unlock usually works.'
    return 'No specific vendor detected. Run \"fastboot getvar all\" and use dry-run first.'

# ---------- Extra features from old tool: Logcat viewer, Device Info ----------
def start_logcat_window(parent, dry_run=False):
    win = tk.Toplevel(parent)
    win.title("Logcat Viewer")
    win.geometry("900x500")
    txt = tk.Text(win, state="disabled")
    txt.pack(fill=tk.BOTH, expand=True)
    btnf = ttk.Frame(win)
    btnf.pack(fill=tk.X)
    stop_flag = {"stop": False}

    def append(txtwidget, s):
        txtwidget.configure(state="normal")
        txtwidget.insert("end", s); txtwidget.see("end"); txtwidget.configure(state="disabled")

    def run_logcat():
        if dry_run:
            append(txt, "[DRY-RUN] logcat not started\n")
            return
        try:
            proc = subprocess.Popen([ADB, "logcat"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
        except Exception as e:
            append(txt, f"Failed start logcat: {e}\n"); return
        with proc_list_lock:
            current_procs.append(proc)
        try:
            for line in proc.stdout:
                if stop_flag["stop"]:
                    break
                append(txt, line)
        except Exception as e:
            append(txt, f"Error reading logcat: {e}\n")
        finally:
            try:
                proc.terminate()
            except Exception:
                pass
            with proc_list_lock:
                try:
                    current_procs.remove(proc)
                except ValueError:
                    pass

    def stop():
        stop_flag["stop"] = True

    ttk.Button(btnf, text="Stop", command=stop).pack(side=tk.LEFT, padx=4)
    threading.Thread(target=run_logcat, daemon=True).start()

def show_device_info(parent):
    info = []
    try:
        res = subprocess.run([ADB, "shell", "getprop"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=4)
        info.append("[adb getprop]\n")
        info.append(res.stdout + "\n")
    except Exception:
        info.append("[adb getprop] failed or no adb device\n")
    try:
        res2 = subprocess.run([FASTBOOT, "getvar", "all"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=4)
        info.append("[fastboot getvar all]\n")
        info.append((res2.stdout or "") + "\n" + (res2.stderr or "") + "\n")
    except Exception:
        info.append("[fastboot getvar all] failed or no fastboot device\n")
    win = tk.Toplevel(parent)
    win.title("Device Info")
    win.geometry("900x600")
    txt = tk.Text(win)
    txt.pack(fill=tk.BOTH, expand=True)
    txt.insert("end", "".join(info))
    txt.configure(state="disabled")

# ---------- MultiFlash Window Class ----------
class MultiFlashWindow:
    def __init__(self, parent, main_term_widget, dryrun_var):
        self.parent = parent
        self.term = main_term_widget
        self.dryrun_var = dryrun_var
        self.win = tk.Toplevel(parent)
        self.win.title("Multi Flash (Batch Flash Tool)")
        self.win.geometry("820x480")
        # allow user to interact with main window (non-modal)
        self.rows = []  # list of dicts: {'frame','chk','part_entry','file_entry','browse_btn','prog'}
        self._build_ui()

    def _build_ui(self):
        ttk.Label(self.win, text="Tambahkan beberapa partition & file untuk diflash berurutan").pack(anchor=tk.W, padx=8, pady=(8,0))

        container = ttk.Frame(self.win)
        container.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        # Scrollable frame for rows
        canvas = tk.Canvas(container)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.configure(yscrollcommand=vsb.set)
        inner = ttk.Frame(canvas)
        inner_id = canvas.create_window((0,0), window=inner, anchor='nw')
        def _on_configure(event):
            canvas.configure(scrollregion=canvas.bbox("all"))
        inner.bind("<Configure>", _on_configure)
        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1*(event.delta/120)), "units")
        canvas.bind_all("<MouseWheel>", _on_mousewheel)

        # header row
        hdr = ttk.Frame(inner)
        hdr.pack(fill=tk.X, pady=2)
        ttk.Label(hdr, text="Sel", width=4).pack(side=tk.LEFT, padx=4)
        ttk.Label(hdr, text="Partition", width=18).pack(side=tk.LEFT, padx=4)
        ttk.Label(hdr, text="File Path", anchor=tk.W).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)
        ttk.Label(hdr, text="Progress", width=16).pack(side=tk.LEFT, padx=8)

        self.rows_container = inner

        # initial two rows
        self.add_row()
        self.add_row()

        # bottom controls
        btnf = ttk.Frame(self.win)
        btnf.pack(fill=tk.X, padx=8, pady=6)
        ttk.Button(btnf, text="+ Add Row", command=self.add_row).pack(side=tk.LEFT)
        ttk.Button(btnf, text="Remove Selected", command=self.remove_selected).pack(side=tk.LEFT, padx=6)
        ttk.Button(btnf, text="Load From File...", command=self.load_from_file).pack(side=tk.LEFT, padx=6)
        ttk.Button(btnf, text="Save To File...", command=self.save_to_file).pack(side=tk.LEFT, padx=6)

        actionf = ttk.Frame(self.win)
        actionf.pack(fill=tk.X, padx=8, pady=(0,8))
        ttk.Button(actionf, text="Start Flash", command=self.start_flash_confirm).pack(side=tk.RIGHT, padx=6)
        ttk.Button(actionf, text="Close", command=self.win.destroy).pack(side=tk.RIGHT)

    def add_row(self, partition_value="", file_value=""):
        rowf = ttk.Frame(self.rows_container)
        rowf.pack(fill=tk.X, pady=3, padx=2)

        var_chk = tk.BooleanVar(value=True)
        chk = ttk.Checkbutton(rowf, variable=var_chk)
        chk.var = var_chk
        chk.pack(side=tk.LEFT, padx=4)

        part_entry = ttk.Entry(rowf, width=20)
        part_entry.insert(0, partition_value)
        part_entry.pack(side=tk.LEFT, padx=4)

        file_entry = ttk.Entry(rowf)
        file_entry.insert(0, file_value)
        file_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)

        def browse_cb():
            fn = filedialog.askopenfilename(title="Select file to flash", filetypes=[("All files","*.*")])
            if fn:
                file_entry.delete(0, tk.END)
                file_entry.insert(0, fn)

        browse_btn = ttk.Button(rowf, text="Browse", command=browse_cb)
        browse_btn.pack(side=tk.LEFT, padx=4)

        prog = ttk.Progressbar(rowf, mode="determinate", length=140)
        prog.pack(side=tk.LEFT, padx=8)

        self.rows.append({
            'frame': rowf,
            'chk': chk,
            'part_entry': part_entry,
            'file_entry': file_entry,
            'browse_btn': browse_btn,
            'prog': prog
        })
        # ensure scrollregion updated
        self.win.update_idletasks()

    def remove_selected(self):
        to_remove = [r for r in self.rows if r['chk'].var.get() is True]
        if not to_remove:
            messagebox.showinfo("Info", "Tidak ada baris yang dipilih untuk dihapus.")
            return
        if not messagebox.askyesno("Confirm", f"Hapus {len(to_remove)} baris terpilih?"):
            return
        for r in to_remove:
            try:
                r['frame'].destroy()
            except Exception:
                pass
            try:
                self.rows.remove(r)
            except ValueError:
                pass

    def load_from_file(self):
        fn = filedialog.askopenfilename(title="Load multi-flash list", filetypes=[("Text files","*.txt;*.csv"),("All files","*.*")])
        if not fn:
            return
        try:
            with open(fn, "r", encoding="utf-8") as f:
                lines = [ln.strip() for ln in f.readlines() if ln.strip()]
            # expected format: partition|filepath  OR partition,filepath OR partition filepath
            for ln in lines:
                if "|" in ln:
                    part, path = ln.split("|",1)
                elif "," in ln:
                    part, path = ln.split(",",1)
                else:
                    parts = ln.split()
                    if len(parts) >= 2:
                        part = parts[0]; path = " ".join(parts[1:])
                    else:
                        continue
                self.add_row(part.strip(), path.strip())
        except Exception as e:
            messagebox.showerror("Error", f"Gagal load file: {e}")

    def save_to_file(self):
        fn = filedialog.asksaveasfilename(defaultextension=".txt", title="Save multi-flash list as", filetypes=[("Text files","*.txt")])
        if not fn:
            return
        try:
            with open(fn, "w", encoding="utf-8") as f:
                for r in self.rows:
                    part = r['part_entry'].get().strip()
                    path = r['file_entry'].get().strip()
                    if part or path:
                        f.write(f"{part}|{path}\n")
            messagebox.showinfo("Saved", f"Saved to {fn}")
        except Exception as e:
            messagebox.showerror("Error", f"Gagal menyimpan: {e}")

    def start_flash_confirm(self):
        # collect selected rows
        selected = [r for r in self.rows if r['chk'].var.get() is True]
        if not selected:
            messagebox.showinfo("Info", "Tidak ada baris yang dipilih untuk diflash.")
            return
        # confirm overall
        if not messagebox.askyesno("Confirm Start", f"Akan mengeksekusi {len(selected)} operasi flash berurutan.\nLanjutkan?"):
            return
        # start worker thread
        threading.Thread(target=self._flash_worker, args=(selected,), daemon=True).start()

    def _flash_worker(self, selected_rows):
        if not is_bin_available(FASTBOOT):
            messagebox.showerror("fastboot missing", "fastboot not found in PATH.")
            return
        fb_list = fastboot_devices_list()
        if not fb_list.strip():
            messagebox.showerror("No device", "No fastboot device detected. Enter bootloader first.")
            return

        for idx, r in enumerate(selected_rows, start=1):
            part = r['part_entry'].get().strip()
            path = r['file_entry'].get().strip()
            prog = r['prog']
            if not part:
                output_q.put(f"[MultiFlash] Skipping row {idx}: partition name kosong.\n")
                continue
            if not path or not os.path.exists(path):
                # ask whether to continue or skip
                if not os.path.exists(path):
                    ans = messagebox.askyesno("File not found", f"File untuk partition '{part}' tidak ditemukan: {path}\nSkip this row and continue?")
                    if ans:
                        output_q.put(f"[MultiFlash] Skipped row {idx} (file not found).\n")
                        continue
                    else:
                        output_q.put(f"[MultiFlash] Aborted by user on missing file for partition {part}.\n")
                        return
            # per-row confirmation
            ok = True
            if not self.dryrun_var.get():
                ok = messagebox.askyesno("Confirm Flash", f"Flash partition '{part}' with file:\n{path}\n\nProceed for this row?")
            else:
                # if dry-run, show info but don't require confirm repeatedly (still show a confirmation)
                ok = messagebox.askyesno("Confirm Dry-Run", f"[DRY-RUN] Would execute: fastboot flash {part} {path}\nProceed for this row?")
            if not ok:
                output_q.put(f"[MultiFlash] User skipped row {idx}: {part}\n")
                continue
            # run command and show progress (indeterminate)
            try:
                output_q.put(f"\n[MultiFlash] Executing row {idx}/{len(selected_rows)}: fastboot flash {part} {path}\n")
                prog.config(mode="indeterminate")
                prog.start(20)
                # use run_cmd_stream to capture and stream output
                ret = run_cmd_stream([FASTBOOT, "flash", part, path], self.term, dry_run=self.dryrun_var.get())
                prog.stop()
                prog.config(mode="determinate")
                if ret == 0:
                    output_q.put(f"[MultiFlash] Row {idx} completed successfully.\n")
                    prog['value'] = 100
                else:
                    output_q.put(f"[MultiFlash] Row {idx} ended with code {ret}.\n")
                    prog['value'] = 0
                    # continue to next row (do not abort automatically)
            except Exception as e:
                prog.stop()
                prog.config(mode="determinate")
                prog['value'] = 0
                output_q.put(f"[MultiFlash] Error on row {idx}: {e}\n")
            # small pause between rows
            time.sleep(0.3)
        output_q.put("\n[MultiFlash] All selected rows processed.\n")

# ---------- GUI App ----------
class SuperDewaApp:
    def __init__(self, root):
        self.root = root
        if BOOTSTRAP_AVAILABLE:
            try:
                self.style = tb.Style(theme='darkly')
            except Exception:
                self.style = None
        else:
            self.style = None

        root.title("ADB & Fastboot by ItsFaa_")
        root.geometry("1180x760")

        # main toolbar
        toolbar = ttk.Frame(root, padding=6)
        toolbar.pack(fill=tk.X)
        ttk.Label(toolbar, text="Mode:").pack(side=tk.LEFT)
        self.mode_var = tk.StringVar(value="ADB")
        ttk.Radiobutton(toolbar, text="ADB", variable=self.mode_var, value="ADB", command=self.rebuild_left).pack(side=tk.LEFT, padx=4)
        ttk.Radiobutton(toolbar, text="Fastboot", variable=self.mode_var, value="Fastboot", command=self.rebuild_left).pack(side=tk.LEFT, padx=4)
        ttk.Button(toolbar, text="Refresh Device Now", command=self.manual_refresh).pack(side=tk.RIGHT, padx=4)

        # main panes
        paned = ttk.Panedwindow(root, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True)
        self.left = ttk.Frame(paned, width=380, padding=8)
        paned.add(self.left, weight=0)
        right = ttk.Frame(paned, padding=8)
        paned.add(right, weight=1)

        # left content (will be built)
        self.dryrun_var = tk.BooleanVar(value=False)
        self.force_var = tk.BooleanVar(value=False)
        self.build_left_adb()

        # Terminal & progress
        ttk.Label(right, text="Terminal Output:").pack(anchor=tk.W)
        self.term = tk.Text(right, state="disabled", wrap="none", height=28, bg="#0b0b0b", fg="#00ff99")
        self.term.pack(fill=tk.BOTH, expand=True)

        # progress area under terminal
        progress_frame = ttk.Frame(right)
        progress_frame.pack(fill=tk.X, pady=(6,0))
        self.progress_label = ttk.Label(progress_frame, text="Status: Idle")
        self.progress_label.pack(side=tk.LEFT)
        self.prog = ttk.Progressbar(progress_frame, mode='indeterminate')
        self.prog.pack(side=tk.RIGHT, fill=tk.X, expand=True)
        self.prog.pack_forget()
        self.prog_running = False

        # bottom buttons
        tbtn = ttk.Frame(right)
        tbtn.pack(fill=tk.X, pady=6)
        ttk.Button(tbtn, text="Clear", command=self.clear_term).pack(side=tk.LEFT)
        ttk.Button(tbtn, text="Stop", command=self.stop_proc).pack(side=tk.LEFT, padx=6)
        ttk.Button(tbtn, text="Save Log", command=self.save_log).pack(side=tk.LEFT, padx=6)
        ttk.Button(tbtn, text="Show Device Info", command=lambda: show_device_info(root)).pack(side=tk.LEFT, padx=6)
        ttk.Button(tbtn, text="Logcat Viewer", command=lambda: start_logcat_window(root, dry_run=self.dryrun_var.get())).pack(side=tk.LEFT, padx=6)

        ttk.Checkbutton(tbtn, text="Dry-run (simulasi)", variable=self.dryrun_var).pack(side=tk.RIGHT, padx=6)
        ttk.Checkbutton(tbtn, text="Force (skip confirmations)", variable=self.force_var).pack(side=tk.RIGHT)

        # status bar
        self.status = ttk.Label(root, text="Initializing...", relief=tk.SUNKEN, anchor=tk.W)
        self.status.pack(fill=tk.X, side=tk.BOTTOM)

        # pollers
        self.root.after(120, self.poll_output)
        self.root.after(500, self.poll_device_state)
        self.root.after(200, self.update_progress_state)

    # build left panels for ADB
    def build_left_adb(self):
        for w in self.left.winfo_children():
            w.destroy()
        ttk.Label(self.left, text="ADB Mode", font=("Segoe UI", 11, "bold")).pack(anchor=tk.W)

        ttk.Button(self.left, text="Check devices", command=self.cmd_devices).pack(fill=tk.X, pady=3)
        ttk.Button(self.left, text="Shell (prompt)", command=self.adb_shell_prompt).pack(fill=tk.X, pady=3)
        ttk.Button(self.left, text="Package Manager", command=self.open_package_manager).pack(fill=tk.X, pady=3)
        ttk.Button(self.left, text="Install APK (choose file)", command=self.adb_install_apk).pack(fill=tk.X, pady=3)
        ttk.Button(self.left, text="Push File → Device", command=self.adb_push_file).pack(fill=tk.X, pady=3)
        ttk.Button(self.left, text="Pull File ← Device", command=self.adb_pull_file).pack(fill=tk.X, pady=3)
        ttk.Button(self.left, text="Backup APK(s) (select packages)", command=self.backup_apks_prompt).pack(fill=tk.X, pady=3)
        ttk.Button(self.left, text="Restore APK(s) (folder or zip)", command=self.restore_apks_prompt).pack(fill=tk.X, pady=3)
        ttk.Button(self.left, text="Presets (Debloat sample)", command=self.run_preset_debloat).pack(fill=tk.X, pady=3)
        ttk.Button(self.left, text="Raw adb command", command=self.adb_raw_prompt).pack(fill=tk.X, pady=3)

        ttk.Label(self.left, text="Reboot / Power Options", font=("Segoe UI", 10, "bold")).pack(anchor=tk.W, pady=(10,3))
        ttk.Button(self.left, text="Reboot System", command=lambda: start_cmd([ADB, "reboot"], self.term, dry_run=self.dryrun_var.get())).pack(fill=tk.X, pady=2)
        ttk.Button(self.left, text="Reboot to Recovery", command=lambda: start_cmd([ADB, "reboot", "recovery"], self.term, dry_run=self.dryrun_var.get())).pack(fill=tk.X, pady=2)
        ttk.Button(self.left, text="Reboot to Bootloader", command=lambda: start_cmd([ADB, "reboot", "bootloader"], self.term, dry_run=self.dryrun_var.get())).pack(fill=tk.X, pady=2)
        ttk.Button(self.left, text="Power Off Device", command=lambda: start_cmd([ADB, "shell", "reboot", "-p"], self.term, dry_run=self.dryrun_var.get())).pack(fill=tk.X, pady=2)

    # build left for fastboot with unlock/lock & auto-flash + MultiFlash
    def build_left_fastboot(self):
        for w in self.left.winfo_children():
            w.destroy()
        ttk.Label(self.left, text="Fastboot Mode", font=("Segoe UI", 11, "bold")).pack(anchor=tk.W)
        ttk.Button(self.left, text="Check devices", command=self.fastboot_devices).pack(fill=tk.X, pady=3)
        ttk.Button(self.left, text="Getvar (all or var)", command=self.fastboot_getvar_prompt).pack(fill=tk.X, pady=3)
        ttk.Button(self.left, text="Flash Partition (.img)", command=self.fastboot_flash_prompt).pack(fill=tk.X, pady=3)
        ttk.Button(self.left, text="Erase Partition", command=self.fastboot_erase_prompt).pack(fill=tk.X, pady=3)
        ttk.Button(self.left, text="Reboot (system/bootloader/recovery/poweroff)", command=self.fastboot_reboot_prompt).pack(fill=tk.X, pady=3)
        ttk.Button(self.left, text="Raw fastboot command", command=self.fastboot_raw_prompt).pack(fill=tk.X, pady=3)

        ttk.Separator(self.left, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=6)
        ttk.Label(self.left, text="Universal Unlock Bootloader", font=("Segoe UI", 10, "bold")).pack(anchor=tk.W, pady=(4,2))
        ttk.Button(self.left, text="Attempt Unlock Bootloader", command=self.attempt_unlock_prompt).pack(fill=tk.X, pady=3)
        ttk.Button(self.left, text="Detect Unlock Method", command=self.show_detect_unlock).pack(fill=tk.X, pady=3)
        ttk.Button(self.left, text="Get fastboot vars (getvar all)", command=self.fastboot_getvar_all_cmd).pack(fill=tk.X, pady=3)
        ttk.Button(self.left, text="Lock Bootloader", command=self.attempt_lock_prompt).pack(fill=tk.X, pady=3)

        ttk.Separator(self.left, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=6)
        ttk.Label(self.left, text="Firmware Tools", font=("Segoe UI", 10, "bold")).pack(anchor=tk.W, pady=(4,2))
        ttk.Button(self.left, text="Auto Flash Firmware ZIP", command=self.auto_flash_zip_prompt).pack(fill=tk.X, pady=3)

        # NEW: Multi Flash (Batch)
        ttk.Button(self.left, text="Multi Flash (Batch)", command=self.open_multi_flash_window).pack(fill=tk.X, pady=3)

        ttk.Label(self.left, text="Reboot / Power Options", font=("Segoe UI", 10, "bold")).pack(anchor=tk.W, pady=(10,3))
        ttk.Button(self.left, text="Reboot System", command=lambda: start_cmd([FASTBOOT, "reboot"], self.term, dry_run=self.dryrun_var.get())).pack(fill=tk.X, pady=2)
        ttk.Button(self.left, text="Reboot to Recovery", command=lambda: start_cmd([FASTBOOT, "reboot", "recovery"], self.term, dry_run=self.dryrun_var.get())).pack(fill=tk.X, pady=2)
        ttk.Button(self.left, text="Reboot to Bootloader", command=lambda: start_cmd([FASTBOOT, "reboot", "bootloader"], self.term, dry_run=self.dryrun_var.get())).pack(fill=tk.X, pady=2)
        ttk.Button(self.left, text="Power Off Device", command=lambda: start_cmd([FASTBOOT, "oem", "poweroff"], self.term, dry_run=self.dryrun_var.get())).pack(fill=tk.X, pady=2)

    def rebuild_left(self):
        if self.mode_var.get() == "ADB":
            self.build_left_adb()
        else:
            self.build_left_fastboot()

    # ---------- terminal helpers ----------
    def clear_term(self):
        self.term.configure(state="normal")
        self.term.delete("1.0", tk.END)
        self.term.configure(state="disabled")

    def save_log(self):
        content = self.term.get("1.0", tk.END)
        if not content.strip():
            messagebox.showinfo("Info", "No output to save.")
            return
        save_text_to_file(content, initial=f"superdewa_log_{timestamp()}.txt")

    def stop_proc(self):
        ok = stop_all_current()
        if ok:
            messagebox.showinfo("Stopped", "Running process(es) terminated.")
        else:
            messagebox.showinfo("Info", "No running process found.")

    def poll_output(self):
        try:
            while True:
                line = output_q.get_nowait()
                append_term(self.term, line)
        except queue.Empty:
            pass
        self.root.after(100, self.poll_output)

    def poll_device_state(self):
        mode, info = detect_device_state()
        if mode == "adb":
            status_text = f"ADB connected — {info}"
        elif mode == "fastboot":
            status_text = f"Fastboot connected — {info}"
        else:
            status_text = "No device connected"
        self.status.config(text=status_text)
        self.root.after(DEVICE_POLL_INTERVAL, self.poll_device_state)

    def manual_refresh(self):
        mode, info = detect_device_state()
        self.status.config(text=(f"{mode}: {info}"))

    # ---------- ADB actions ----------
    def cmd_devices(self):
        if not is_bin_available(ADB):
            messagebox.showerror("adb missing", "adb not found in PATH.")
            return
        start_cmd([ADB, "devices", "-l"], self.term, dry_run=self.dryrun_var.get())

    def adb_shell_prompt(self):
        if not is_bin_available(ADB):
            messagebox.showerror("adb missing", "adb not found.")
            return
        cmd = simpledialog.askstring("ADB Shell", "Enter shell command (e.g. pm list packages):")
        if cmd:
            start_cmd([ADB, "shell"] + cmd.split(), self.term, dry_run=self.dryrun_var.get())

    def adb_install_apk(self):
        if not is_bin_available(ADB):
            messagebox.showerror("adb missing", "adb not found.")
            return
        apk = filedialog.askopenfilename(title="Select APK to install", filetypes=[("APK","*.apk")])
        if apk:
            start_cmd([ADB, "install", "-r", apk], self.term, dry_run=self.dryrun_var.get())

    def adb_push_file(self):
        if not is_bin_available(ADB):
            messagebox.showerror("adb missing", "adb not found.")
            return
        src = filedialog.askopenfilename(title="Select file to push to device")
        if not src:
            return
        dest = simpledialog.askstring("Push to device", "Enter destination path on device (e.g. /sdcard/):", initialvalue="/sdcard/")
        if not dest:
            return
        start_cmd([ADB, "push", src, dest], self.term, dry_run=self.dryrun_var.get())

    def adb_pull_file(self):
        if not is_bin_available(ADB):
            messagebox.showerror("adb missing", "adb not found.")
            return
        src = simpledialog.askstring("Pull from device", "Enter source path on device (e.g. /sdcard/DCIM/Camera):")
        if not src:
            return
        dest = filedialog.askdirectory(title="Select destination folder on PC")
        if not dest:
            return
        start_cmd([ADB, "pull", src, dest], self.term, dry_run=self.dryrun_var.get())

    def adb_raw_prompt(self):
        if not is_bin_available(ADB):
            messagebox.showerror("adb missing", "adb not found.")
            return
        args = simpledialog.askstring("Raw adb", "Enter adb args (without 'adb'):")
        if args:
            start_cmd([ADB] + args.split(), self.term, dry_run=self.dryrun_var.get())

    # ---------- Package manager UI ----------
    def open_package_manager(self):
        if not is_bin_available(ADB):
            messagebox.showerror("adb missing", "adb not found in PATH.")
            return
        win = tk.Toplevel(self.root)
        win.title("Package Manager")
        win.geometry("700x480")
        ttk.Label(win, text="Packages (pm list packages)").pack(anchor=tk.W, padx=6, pady=4)
        search_var = tk.StringVar()
        search_entry = ttk.Entry(win, textvariable=search_var)
        search_entry.pack(fill=tk.X, padx=6)

        listbox = tk.Listbox(win, selectmode=tk.EXTENDED)
        listbox.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        btnf = ttk.Frame(win)
        btnf.pack(fill=tk.X, padx=6, pady=6)
        ttk.Button(btnf, text="Refresh", command=lambda: self.pm_refresh(listbox, search_var)).pack(side=tk.LEFT)
        ttk.Button(btnf, text="Uninstall (user 0)", command=lambda: self.pm_uninstall_selected(listbox)).pack(side=tk.LEFT, padx=6)
        ttk.Button(btnf, text="Disable", command=lambda: self.pm_disable_selected(listbox)).pack(side=tk.LEFT, padx=6)
        ttk.Button(btnf, text="Enable", command=lambda: self.pm_enable_selected(listbox)).pack(side=tk.LEFT, padx=6)
        ttk.Button(btnf, text="Backup selected APKs", command=lambda: self.pm_backup_selected(listbox)).pack(side=tk.RIGHT)

        self.pm_refresh(listbox, search_var)

        def on_search(*a):
            term = search_var.get().lower()
            items = getattr(listbox, "_allpkgs", [])
            listbox.delete(0, tk.END)
            for p in items:
                if term in p.lower():
                    listbox.insert(tk.END, p)
        search_var.trace_add("write", lambda *a: on_search())

    def pm_refresh(self, listbox, search_var):
        try:
            res = subprocess.run([ADB, "shell", "pm", "list", "packages"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=8)
            lines = [ln.split("package:")[-1].strip() for ln in res.stdout.splitlines() if ln.strip()]
        except Exception as e:
            messagebox.showerror("Error", f"Failed to list packages: {e}")
            lines = []
        listbox._allpkgs = lines
        q = search_var.get().lower()
        listbox.delete(0, tk.END)
        for p in lines:
            if q in p.lower():
                listbox.insert(tk.END, p)

    def pm_uninstall_selected(self, listbox):
        sel = listbox.curselection()
        if not sel:
            messagebox.showinfo("Info", "No package selected.")
            return
        packages = [listbox.get(i) for i in sel]
        if not messagebox.askyesno("Confirm", f"Uninstall {len(packages)} package(s) for user 0?"):
            return
        for pkg in packages:
            start_cmd([ADB, "shell", "pm", "uninstall", "-k", "--user", "0", pkg], self.term, dry_run=self.dryrun_var.get())
            time.sleep(0.08)

    def pm_disable_selected(self, listbox):
        sel = listbox.curselection()
        if not sel:
            messagebox.showinfo("Info", "No package selected.")
            return
        pkgs = [listbox.get(i) for i in sel]
        for p in pkgs:
            start_cmd([ADB, "shell", "pm", "disable-user", "--user", "0", p], self.term, dry_run=self.dryrun_var.get())
            time.sleep(0.08)

    def pm_enable_selected(self, listbox):
        sel = listbox.curselection()
        if not sel:
            messagebox.showinfo("Info", "No package selected.")
            return
        pkgs = [listbox.get(i) for i in sel]
        for p in pkgs:
            start_cmd([ADB, "shell", "pm", "enable", p], self.term, dry_run=self.dryrun_var.get())
            time.sleep(0.08)

    def pm_backup_selected(self, listbox):
        sel = listbox.curselection()
        if not sel:
            messagebox.showinfo("Info", "No package selected.")
            return
        pkgs = [listbox.get(i) for i in sel]
        folder = filedialog.askdirectory(title="Select folder to save APK backups")
        if not folder:
            return
        for pkg in pkgs:
            threading.Thread(target=self._backup_apk_worker_with_progress, args=(pkg, folder), daemon=True).start()

    def _backup_apk_worker_with_progress(self, package, folder):
        progress = ProgressDialog(self.root, f"Backing up {package} ...")
        try:
            try:
                res = subprocess.run([ADB, "shell", "pm", "path", package], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
                out = res.stdout.strip()
            except Exception as e:
                output_q.put(f"Error getting pm path for {package}: {e}\n")
                progress.close(); return
            if not out:
                output_q.put(f"Package {package} path not found or no permission.\n")
                progress.close(); return
            paths = [ln.split("package:")[-1].strip() for ln in out.splitlines() if ln.strip()]
            for pth in paths:
                base = os.path.basename(pth)
                dest = os.path.join(folder, f"{package}__{base}")
                run_cmd_stream([ADB, "pull", pth, dest], self.term, dry_run=self.dryrun_var.get())
            output_q.put(f"Backup finished for {package}\n")
        finally:
            progress.close()

    # ---------- Backup/Restore ----------
    def backup_apks_prompt(self):
        pkgs = simpledialog.askstring("Backup APKs", "Enter package names separated by commas (or leave blank to open Package Manager):")
        if not pkgs:
            self.open_package_manager(); return
        packages = [p.strip() for p in pkgs.split(",") if p.strip()]
        folder = filedialog.askdirectory(title="Save folder for APKs")
        if not folder: return
        for pkg in packages:
            threading.Thread(target=self._backup_apk_worker_with_progress, args=(pkg, folder), daemon=True).start()

    def restore_apks_prompt(self):
        fn = filedialog.askopenfilename(title="Select APK file or ZIP (or cancel to choose folder)", filetypes=[("APK",".apk"),("ZIP",".zip"),("All files",".*")])
        if not fn:
            folder = filedialog.askdirectory(title="Select folder containing APKs")
            if not folder: return
            for f in os.listdir(folder):
                if f.lower().endswith(".apk"):
                    start_cmd([ADB, "install", "-r", os.path.join(folder, f)], self.term, dry_run=self.dryrun_var.get())
            return
        if fn.lower().endswith(".zip"):
            tmp = filedialog.askdirectory(title="Select temp extract folder")
            if not tmp: return
            try:
                with zipfile.ZipFile(fn, "r") as z:
                    z.extractall(tmp)
                for rootp, dirs, files in os.walk(tmp):
                    for f in files:
                        if f.lower().endswith(".apk"):
                            start_cmd([ADB, "install", "-r", os.path.join(rootp, f)], self.term, dry_run=self.dryrun_var.get())
            except Exception as e:
                messagebox.showerror("Error", f"Failed extract/install: {e}")
        elif fn.lower().endswith(".apk"):
            start_cmd([ADB, "install", "-r", fn], self.term, dry_run=self.dryrun_var.get())
        else:
            messagebox.showinfo("Info", "Selected unsupported file. Choose APK or ZIP or a folder.")

    # ---------- Presets ----------
    def run_preset_debloat(self):
        if not is_bin_available(ADB):
            messagebox.showerror("adb missing", "adb not found in PATH."); return
        if not PRESET_DEBLOAT:
            messagebox.showinfo("Info", "No packages listed in preset."); return
        if not messagebox.askyesno("Confirm", f"Run debloat preset on {len(PRESET_DEBLOAT)} packages?"): return
        for pkg in PRESET_DEBLOAT:
            start_cmd([ADB, "shell", "pm", "uninstall", "-k", "--user", "0", pkg], self.term, dry_run=self.dryrun_var.get())
            time.sleep(0.08)

    # ---------- Fastboot actions ----------
    def fastboot_devices(self):
        if not is_bin_available(FASTBOOT):
            messagebox.showerror("fastboot missing", "fastboot not found in PATH."); return
        start_cmd([FASTBOOT, "devices"], self.term, dry_run=self.dryrun_var.get())

    def fastboot_getvar_prompt(self):
        if not is_bin_available(FASTBOOT):
            messagebox.showerror("fastboot missing", "fastboot not found."); return
        var = simpledialog.askstring("fastboot getvar", "Enter var (e.g. all or product):", initialvalue="all")
        if var:
            start_cmd([FASTBOOT, "getvar", var], self.term, dry_run=self.dryrun_var.get())

    def fastboot_flash_prompt(self):
        if not is_bin_available(FASTBOOT):
            messagebox.showerror("fastboot missing", "fastboot not found."); return
        img = filedialog.askopenfilename(title="Select .img to flash", filetypes=[("IMG",".img"),("All files",".*")])
        if not img: return
        name = os.path.basename(img).lower()
        guessed = None
        for k in ("boot", "recovery", "system", "vbmeta", "vendor", "odm", "product"):
            if k in name:
                guessed = k; break
        part = simpledialog.askstring("Partition", f"Partition to flash (guessed: {guessed}):", initialvalue=(guessed or "boot"))
        if part and messagebox.askyesno("Confirm", f"Flash {os.path.basename(img)} to partition {part}?"):
            start_cmd([FASTBOOT, "flash", part, img], self.term, dry_run=self.dryrun_var.get())

    def fastboot_erase_prompt(self):
        if not is_bin_available(FASTBOOT):
            messagebox.showerror("fastboot missing", "fastboot not found."); return
        part = simpledialog.askstring("Erase", "Partition to erase (e.g. userdata):", initialvalue="userdata")
        if part and messagebox.askyesno("Confirm", f"Erase partition {part}? This is destructive."):
            start_cmd([FASTBOOT, "erase", part], self.term, dry_run=self.dryrun_var.get())

    def fastboot_reboot_prompt(self):
        if not is_bin_available(FASTBOOT):
            messagebox.showerror("fastboot missing", "fastboot not found."); return
        mode = simpledialog.askstring("Reboot", "Target (reboot / bootloader / recovery / poweroff):", initialvalue="reboot")
        if mode:
            start_cmd([FASTBOOT, "reboot", mode], self.term, dry_run=self.dryrun_var.get())

    def fastboot_raw_prompt(self):
        if not is_bin_available(FASTBOOT):
            messagebox.showerror("fastboot missing", "fastboot not found."); return
        args = simpledialog.askstring("Raw fastboot", "Enter args (without 'fastboot'):")
        if args:
            start_cmd([FASTBOOT] + args.split(), self.term, dry_run=self.dryrun_var.get())

    # ---------- Unlock/Lock UI triggers ----------
    def attempt_unlock_prompt(self):
        if not is_bin_available(FASTBOOT):
            messagebox.showerror("fastboot missing", "fastboot not found in PATH."); return
        fb_list = fastboot_devices_list()
        if not fb_list.strip():
            messagebox.showerror("No device", "No fastboot device detected. Enter bootloader first."); return

        summary = ("This will try common fastboot commands to unlock bootloader.\n"
                   "WARNING: this usually ERASES DATA and may void warranty.\n\n"
                   f"Dry-run: {self.dryrun_var.get()}\n"
                   f"Force: {self.force_var.get()}\n\n"
                   "Continue?")
        if not messagebox.askyesno("Confirm", summary):
            return

        default_log = f"unlock_log_{timestamp()}.txt"
        logfile = filedialog.asksaveasfilename(defaultextension=".txt", initialfile=default_log, title="Save unlock log as (or cancel to skip)")
        if logfile == "":
            logfile = None

        threading.Thread(target=unlock_worker, args=(self.term, self.dryrun_var.get(), self.force_var.get(), logfile), daemon=True).start()

    def attempt_lock_prompt(self):
        if not is_bin_available(FASTBOOT):
            messagebox.showerror("fastboot missing", "fastboot not found in PATH."); return
        fb_list = fastboot_devices_list()
        if not fb_list.strip():
            messagebox.showerror("No device", "No fastboot device detected. Enter bootloader first."); return

        summary = ("Locking the bootloader may ERASE DATA and make device secure again.\n"
                   f"Dry-run: {self.dryrun_var.get()}\n"
                   f"Force: {self.force_var.get()}\n\n"
                   "Continue?")
        if not messagebox.askyesno("Confirm Lock", summary):
            return

        threading.Thread(target=lock_worker, args=(self.term, self.dryrun_var.get(), self.force_var.get()), daemon=True).start()

    def fastboot_getvar_all_cmd(self):
        if not is_bin_available(FASTBOOT):
            messagebox.showerror("fastboot missing", "fastboot not found."); return
        try:
            res = subprocess.run([FASTBOOT, "getvar", "all"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=6)
            combined = (res.stdout or "") + "\n" + (res.stderr or "")
            output_q.put(combined + "\n")
        except Exception as e:
            output_q.put(f"Error running getvar all: {e}\n")

    def auto_flash_zip_prompt(self):
        if not is_bin_available(FASTBOOT):
            messagebox.showerror("fastboot missing", "fastboot not found in PATH."); return
        fn = filedialog.askopenfilename(title="Select firmware ZIP to auto-flash", filetypes=[("ZIP",".zip")])
        if not fn: return
        threading.Thread(target=auto_flash_zip_worker, args=(self.term, fn, self.dryrun_var.get()), daemon=True).start()

    def show_detect_unlock(self):
        suggestion = detect_unlock_suggestion()
        messagebox.showinfo("Detect Unlock Method", suggestion)

    # ---------- MultiFlash window launcher ----------
    def open_multi_flash_window(self):
        MultiFlashWindow(self.root, self.term, self.dryrun_var)

    # ---------- Progress control ----------
    def update_progress_state(self):
        with proc_list_lock:
            running = any((p.poll() is None) for p in current_procs) if current_procs else False
        if running and not self.prog_running:
            try:
                self.prog.pack(side=tk.RIGHT, fill=tk.X, expand=True)
                self.prog.start(20)
                self.progress_label.config(text="Status: Running tasks...")
                self.prog_running = True
            except Exception:
                pass
        elif not running and self.prog_running:
            try:
                self.prog.stop()
                self.prog.pack_forget()
                self.progress_label.config(text="Status: Idle")
                self.prog_running = False
            except Exception:
                pass
        self.root.after(200, self.update_progress_state)

# ---------- ProgressDialog ----------
class ProgressDialog:
    def __init__(self, parent, title="Working..."):
        self.top = tk.Toplevel(parent)
        self.top.title(title)
        self.top.geometry("360x96")
        self.top.transient(parent)
        self.top.grab_set()
        ttk.Label(self.top, text=title).pack(pady=8)
        self.pb = ttk.Progressbar(self.top, mode="indeterminate")
        self.pb.pack(fill=tk.X, padx=12, pady=6)
        self.pb.start(20)
        self.top.protocol("WM_DELETE_WINDOW", lambda: None)

    def close(self):
        try:
            self.pb.stop()
            self.top.grab_release()
            self.top.destroy()
        except Exception:
            pass

# ---------- main ----------
def main():
    if BOOTSTRAP_AVAILABLE:
        try:
            root = tb.Window(themename='darkly')
        except Exception:
            root = tk.Tk()
    else:
        root = tk.Tk()
    app = SuperDewaApp(root)
    root.mainloop()

if __name__ == "__main__":
    main()
