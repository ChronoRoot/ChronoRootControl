import os
import shutil
import subprocess
import json
import time
from flask import Blueprint, render_template, send_from_directory, abort, request, flash, url_for
from config import Config

storage_page = Blueprint('storage_page', __name__,
                         template_folder='templates',
                         static_folder='static')

@storage_page.context_processor
def inject_config():
    """
    Makes the 'config' variable available to all templates 
    rendered by this blueprint (fixing the missing menu text).
    """
    return dict(config=Config)

def get_folder_stats(path):
    """Calculates total size and file count of a directory."""
    total_size = 0
    file_count = 0
    try:
        for dirpath, dirnames, filenames in os.walk(path):
            file_count += len(filenames)
            for f in filenames:
                fp = os.path.join(dirpath, f)
                total_size += os.path.getsize(fp)
    except Exception:
        pass
    
    # Convert to readable format
    if total_size > 1024**3:
        size_str = f"{round(total_size / (1024**3), 2)} GB"
    else:
        size_str = f"{round(total_size / (1024**2), 1)} MB"
        
    return size_str, file_count

def get_mounted_drives():
    """
    Scans Linux /proc/mounts to find external drives (usually /media or /mnt).
    Returns a list of available paths.
    """
    drives = []
    
    # Always add the default home directory option
    internal_root = os.path.expanduser("~")
    drives.append({
        "device": "Internal SD",
        "mountpoint": "/srv/ChronoRootData", 
        "type": "ext4"
    })

    try:
        with open('/proc/mounts', 'r') as f:
            for line in f:
                parts = line.split()
                if len(parts) > 2:
                    device, mountpoint, fstype = parts[0], parts[1], parts[2]
                    # Filter for interesting drives (USB, HDD)
                    if mountpoint.startswith(('/media', '/mnt', '/run/media')) and not mountpoint.startswith('/mnt/wsl'):
                        drives.append({
                            "device": device,
                            "mountpoint": mountpoint,
                            "type": fstype
                        })
    except Exception:
        pass
        
    return drives

def get_block_devices():
    """
    Runs lsblk to find all partition-type devices that are NOT mounted.
    Returns a list of dicts suitable for the frontend.
    """
    cmd = ["lsblk", "-J", "-o", "NAME,SIZE,TYPE,MOUNTPOINT,UUID,FSTYPE,LABEL"]
    try:
        output = subprocess.check_output(cmd).decode('utf-8')
        data = json.loads(output)
        
        candidates = []
        
        def process_device(dev):
            # We want partitions ('part') that have NO mountpoint
            if dev.get('type') == 'part' and not dev.get('mountpoint'):
                candidates.append({
                    "name": dev.get('name'),       # e.g. sda1
                    "size": dev.get('size'),
                    "fstype": dev.get('fstype'),
                    "uuid": dev.get('uuid'),
                    "label": dev.get('label')
                })
        
        for device in data.get('blockdevices', []):
            # Check partitions inside devices (sda -> sda1)
            if device.get('children'):
                for child in device['children']:
                    process_device(child)
            else:
                # Check direct partitions
                process_device(device)
                
        return candidates
    except Exception as e:
        print(f"Error scanning disks: {e}")
        return []

# --- ROUTES ---

@storage_page.route('/')
def index():
    path = Config.WORKING_DIR
    
    # 1. Disk Usage (Same as before)
    try:
        total, used, free = shutil.disk_usage(path)
        percent = (used / total) * 100
        is_mounted = os.stat('/').st_dev != os.stat(path).st_dev
    except FileNotFoundError:
        total, used, free, percent, is_mounted = 0, 0, 0, 0, False

    info = {
        "path": path,
        "total_gb": round(total / (2**30), 2),
        "free_gb": round(free / (2**30), 2),
        "percent": round(percent, 1),
        "used_gb": round(used / (2**30), 2),
        "is_mounted": is_mounted
    }
    
    # 2. List ALL Content (Files & Folders)
    items = []
    if os.path.exists(path):
        for name in os.listdir(path):
            full_path = os.path.join(path, name)
            try:
                stats = os.stat(full_path)
                is_dir = os.path.isdir(full_path)
                
                if is_dir:
                    # It's a Folder: Count children
                    size_str, count = get_folder_stats(full_path)
                    files_label = f"{count} items"
                else:
                    # It's a File: Get direct size
                    if stats.st_size > 1024**3:
                        size_str = f"{round(stats.st_size / (1024**3), 2)} GB"
                    elif stats.st_size > 1024**2:
                        size_str = f"{round(stats.st_size / (1024**2), 1)} MB"
                    else:
                        size_str = f"{round(stats.st_size / 1024, 1)} KB"
                    files_label = "File"

                items.append({
                    "name": name,
                    "modified": stats.st_mtime,
                    "size": size_str,
                    "files": files_label,
                    "is_dir": is_dir  # <--- Important Flag
                })
            except OSError:
                continue

        # Sort by newest modified date
        items.sort(key=lambda x: x['modified'], reverse=True)

    # 3. Get Mounts (Same as before)
    mounts = get_mounted_drives()
    unmounted = get_block_devices()

    # Pass 'experiments' as 'items' to match the template variable name
    return render_template('storage.html', 
                           info=info, 
                           experiments=items, 
                           mounts=mounts, 
                           unmounted=unmounted)

@storage_page.route('/set_path', methods=['POST'])
def set_path():
    """
    Updates the WORKING_DIR by writing to 'user_config.py'.
    This overrides the defaults without modifying default_config.py.
    """
    new_path = request.form.get('new_path')
    
    if not new_path:
        flash("No path selected.", "danger")
        target_url = url_for('storage_page.index')
        return f"<script>window.location.href = '{target_url}';</script>"

    # 1. Locate where user_config.py should be
    # It must be in the same folder as config.py
    project_root = "/srv/ChronoRootControl"
    user_config_file = os.path.join(project_root, 'user_config.py')

    # 2. Check Permissions (if file exists)
    if os.path.exists(user_config_file) and not os.access(user_config_file, os.W_OK):
        flash(f"Permission Error: Cannot write to {user_config_file}. Check file ownership.", "danger")
        target_url = url_for('storage_page.index')
        return f"<script>window.location.href = '{target_url}';</script>"

    try:
        # 3. Read existing file or start empty
        lines = []
        if os.path.exists(user_config_file):
            with open(user_config_file, 'r') as f:
                lines = f.readlines()

        # 4. Analyze content
        has_class = False
        var_found = False
        new_lines = []
        
        # Check if 'class Config' exists
        for line in lines:
            if "class Config" in line:
                has_class = True
            if "WORKING_DIR =" in line:
                var_found = True
                # Replace the existing line, preserving indentation
                prefix = line.split("WORKING_DIR")[0]
                new_lines.append(f"{prefix}WORKING_DIR = '{new_path}'\n")
            else:
                new_lines.append(line)

        # 5. Logic to Insert if missing
        if not has_class:
            # File is empty or has no Config class -> Create it
            new_lines.append("\nclass Config(object):\n")
            new_lines.append(f"    WORKING_DIR = '{new_path}'\n")
        
        elif has_class and not var_found:
            # Class exists but WORKING_DIR is not inside it -> Insert it
            # We insert it right after the "class Config" line
            final_lines = []
            for line in new_lines:
                final_lines.append(line)
                if "class Config" in line:
                    final_lines.append(f"    WORKING_DIR = '{new_path}'\n")
            new_lines = final_lines

        # 6. Write back to file
        with open(user_config_file, 'w') as f:
            f.writelines(new_lines)
            
        flash(f"Success! Storage path set to {new_path}.", "success")

    except Exception as e:
        flash(f"System Error updating config: {e}", "danger")
        
    return render_template('restarting.html', target_url=url_for('storage_page.index'))

@storage_page.route('/trigger_restart')
def trigger_restart():
    """
    This route is called via AJAX by the restarting.html page.
    It executes the shell command to kill/restart the service.
    """
    
    time.sleep(1) # Give the server a moment to send the 200 OK response
    subprocess.run(["sudo", "systemctl", "restart", "uwsgi"])
        
    return "Restarting..."

@storage_page.route('/mount_drive', methods=['POST'])
def mount_drive():
    """
    Mounts a drive with FULL WRITE PERMISSIONS (777) for all users.
    Handles FAT32/NTFS (via mount options) and Ext4 (via chmod).
    """
    device_name = request.form.get('device_name') # e.g. sda1
    device_uuid = request.form.get('device_uuid')
    fstype = request.form.get('fstype')
    label = request.form.get('label')
    
    # Safety: Ensure label is clean
    safe_label = "".join([c for c in label if c.isalnum() or c in "._-"]) if label else "usb_drive"
    mount_point = f"/media/pi/{safe_label}"
    
    if not device_uuid:
        flash("Error: Drive has no UUID. Cannot mount persistently.", "danger")
        target_url = url_for('storage_page.index')
        return f"<script>window.location.href = '{target_url}';</script>"

    try:
        # Step 1: Create Mount Point
        if not os.path.exists(mount_point):
            subprocess.run(["sudo", "mkdir", "-p", mount_point], check=True)
            # Set parent folder permissions just in case
            subprocess.run(["sudo", "chmod", "777", mount_point], check=True)

        # Step 2: Determine Mount Options based on Filesystem
        # FAT/NTFS need explicit umask to allow writing. Ext4 ignores umask.
        mount_options = "defaults,nofail"
        cmd_options = []
        
        is_windows_fs = fstype in ['vfat', 'ntfs', 'exfat', 'fat32']
        
        if is_windows_fs:
            # umask=000 gives 777 permissions (rwxrwxrwx) to everyone
            mount_options += ",umask=000"
            cmd_options = ["-o", "umask=000"]
        
        # Step 3: Mount immediately
        dev_path = f"/dev/{device_name}"
        mount_cmd = ["sudo", "mount"] + cmd_options + [dev_path, mount_point]
        subprocess.run(mount_cmd, check=True)
        
        # Step 4: Fix Permissions for Linux Filesystems (Ext4)
        # For Ext4, we must change permissions AFTER mounting
        if not is_windows_fs:
            try:
                # Option A: Give ownership to chronoroot user (if it exists)
                subprocess.run(["sudo", "chown", "-R", "chronoroot:chronoroot", mount_point], check=False)
                # Option B: Just make it writable for everyone (safer for shared web/app access)
                subprocess.run(["sudo", "chmod", "-R", "777", mount_point], check=True)
            except Exception as e:
                print(f"Permission fix warning: {e}")

        # Step 5: Persist in /etc/fstab
        fstab_line = f"UUID={device_uuid} {mount_point} {fstype} {mount_options} 0 2"
        
        # Check for duplicates
        with open("/etc/fstab", "r") as f:
            if device_uuid in f.read():
                flash(f"Mounted at {mount_point}. (Fstab entry already existed)", "success")
            else:
                # Append securely
                cmd = f"echo '{fstab_line}' | sudo tee -a /etc/fstab"
                subprocess.run(cmd, shell=True, check=True)
                flash(f"Success! Mounted at {mount_point} with write permissions.", "success")

    except subprocess.CalledProcessError as e:
        flash(f"Mount command failed: {e}", "danger")
    except Exception as e:
        flash(f"Error: {e}", "danger")
        
    target_url = url_for('storage_page.index')
    return f"<script>window.location.href = '{target_url}';</script>"

@storage_page.route('/browse/<path:subpath>')
def browse(subpath):
    """
    Simple file browser for inside an experiment folder.
    """
    req_path = os.path.join(Config.WORKING_DIR, subpath)
    
    # Security Check: Prevent directory traversal (../../)
    if not os.path.realpath(req_path).startswith(os.path.realpath(Config.WORKING_DIR)):
        return abort(403)

    if os.path.isdir(req_path):
        files = os.listdir(req_path)
        contents = []
        for f in sorted(files):
            f_path = os.path.join(req_path, f)
            is_dir = os.path.isdir(f_path)
            size = 0
            if not is_dir:
                try:
                    size = os.path.getsize(f_path)
                except:
                    pass
            
            contents.append({
                "name": f,
                "is_dir": is_dir,
                "rel_path": os.path.join(subpath, f),
                "size": size
            })
        return render_template('file_browser.html', current_path=subpath, contents=contents)
    
    else:
        # Serve file
        directory = os.path.dirname(req_path)
        filename = os.path.basename(req_path)
        return send_from_directory(directory, filename)