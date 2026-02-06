"""Tkinter GUI for managing permissions and roles."""

import json
import os
import sys
import tkinter as tk
from pathlib import Path


def get_state_dir():
    return Path.home() / ".local" / "state" / "kitty-claude"


def load_permissions(session_config_dir, cwd):
    """Load permissions from session settings + project settings.local.json.
    Returns list of (rule, source_label, source_file_path)."""
    rules = []

    settings_file = Path(session_config_dir) / "settings.json"
    if settings_file.exists():
        try:
            settings = json.loads(settings_file.read_text())
            for rule in settings.get("permissions", {}).get("allow", []):
                rules.append((rule, "session", str(settings_file)))
        except (json.JSONDecodeError, OSError):
            pass

    project_settings = Path(cwd) / ".claude" / "settings.local.json"
    if project_settings.exists():
        try:
            proj = json.loads(project_settings.read_text())
            for rule in proj.get("permissions", {}).get("allow", []):
                rules.append((rule, "project", str(project_settings)))
        except (json.JSONDecodeError, OSError):
            pass

    # Deduplicate preserving order
    seen = set()
    unique = []
    for rule, label, source in rules:
        if rule not in seen:
            seen.add(rule)
            unique.append((rule, label, source))
    return unique


def load_roles(roles_dir):
    """Load all roles. Returns dict of role_name -> role_data."""
    roles = {}
    if roles_dir.exists():
        for f in sorted(roles_dir.glob("*.json")):
            try:
                roles[f.stem] = json.loads(f.read_text())
            except (json.JSONDecodeError, OSError):
                roles[f.stem] = {"mcpServers": {}, "permissions": {"allow": []}}
    return roles


def save_roles(roles_dir, roles):
    """Save all role files."""
    roles_dir.mkdir(parents=True, exist_ok=True)
    for name, data in roles.items():
        role_file = roles_dir / f"{name}.json"
        role_file.write_text(json.dumps(data, indent=2))


def get_tmux_window_name():
    """Get the current tmux window name."""
    import subprocess
    tmux_socket = os.environ.get('KITTY_CLAUDE_TMUX_SOCKET', 'kitty-claude')
    try:
        result = subprocess.run(
            ["tmux", "-L", tmux_socket, "display-message", "-p", "#{window_name}"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except:
        pass
    return None


def run_gui(session_config_dir, cwd, roles_dir, config_dir=None, session_id=None):
    permissions = load_permissions(session_config_dir, cwd)
    all_roles = load_roles(roles_dir)

    # Only show active roles for this session
    active_role_names = []
    if session_id:
        state_dir = get_state_dir()
        metadata_file = state_dir / "sessions" / f"{session_id}.json"
        if metadata_file.exists():
            try:
                metadata = json.loads(metadata_file.read_text())
                active_role_names = metadata.get("activeRoles", [])
            except (json.JSONDecodeError, OSError):
                pass

    # Add implicit roles: default (if exists) and title-based roles
    if config_dir:
        config_path = Path(config_dir)
        # Default role
        if "default" in all_roles and "default" not in active_role_names:
            active_role_names.insert(0, "default")
        # Title-based roles
        title_roles_file = config_path / "title-roles.json"
        if title_roles_file.exists():
            try:
                title_mappings = json.loads(title_roles_file.read_text())
                window_name = get_tmux_window_name()
                if window_name and window_name in title_mappings:
                    for role_name in title_mappings[window_name]:
                        if role_name not in active_role_names and role_name in all_roles:
                            active_role_names.append(role_name)
            except (json.JSONDecodeError, OSError):
                pass

    # Filter to active roles only (preserving role data)
    roles = {name: all_roles.get(name, {"mcpServers": {}, "permissions": {"allow": []}}) for name in active_role_names if name in all_roles}
    role_names = list(roles.keys())

    # Load title-role mappings
    title_roles_file = Path(config_dir) / "title-roles.json" if config_dir else None
    title_mappings = {}
    if title_roles_file and title_roles_file.exists():
        try:
            title_mappings = json.loads(title_roles_file.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    window_title = get_tmux_window_name() or "(unknown)"

    root = tk.Tk()
    root.title("Permissions Editor")
    root.geometry("900x600")

    # --- Top spacer ---
    tk.Frame(root).pack(pady=(5, 0))

    def remove_permission(idx):
        """Remove a permission from its source file and the table."""
        rule, source_label, source_file = permissions[idx]
        try:
            source_path = Path(source_file)
            data = json.loads(source_path.read_text())
            allow_list = data.get("permissions", {}).get("allow", [])
            if rule in allow_list:
                allow_list.remove(rule)
                source_path.write_text(json.dumps(data, indent=2))
        except (json.JSONDecodeError, OSError):
            pass
        sync_checkboxes_to_roles()
        permissions.pop(idx)
        rebuild_table()

    def sync_checkboxes_to_roles():
        """Capture current checkbox state into roles dict."""
        for rname in role_names:
            role_data = roles.get(rname, {"mcpServers": {}, "permissions": {"allow": []}})
            new_allow = []
            for rule, _source, _path in permissions:
                var = checkbox_vars.get((rule, rname))
                if var and var.get():
                    new_allow.append(rule)
            role_data.setdefault("permissions", {})["allow"] = new_allow
            roles[rname] = role_data

    # --- Title-role mappings ---
    title_frame = tk.LabelFrame(root, text="Title-role mappings", padx=5, pady=5)
    title_frame.pack(fill=tk.X, padx=10, pady=(0, 5))

    title_inner = tk.Frame(title_frame)
    title_inner.pack(fill=tk.X)

    def save_title_mappings():
        if title_roles_file:
            title_roles_file.write_text(json.dumps(title_mappings, indent=2))

    def rebuild_title_section():
        for widget in title_inner.winfo_children():
            widget.destroy()

        # Current title (read-only)
        tk.Label(title_inner, text=f"Window: {window_title}", font=("monospace", 9, "bold"),
                 anchor=tk.W).grid(row=0, column=0, sticky=tk.W, columnspan=2)

        # Current roles for this title
        current_roles = title_mappings.get(window_title, [])
        if current_roles:
            roles_frame = tk.Frame(title_inner)
            roles_frame.grid(row=1, column=0, columnspan=10, sticky=tk.W, pady=(2, 0))
            tk.Label(roles_frame, text="Roles:", font=("monospace", 9)).pack(side=tk.LEFT)
            for r in current_roles:
                def make_remove(role=r):
                    def remove():
                        title_mappings[window_title].remove(role)
                        if not title_mappings[window_title]:
                            del title_mappings[window_title]
                        save_title_mappings()
                        rebuild_title_section()
                    return remove
                tk.Label(roles_frame, text=r, font=("monospace", 9)).pack(side=tk.LEFT, padx=(5, 0))
                tk.Button(roles_frame, text="x", font=("monospace", 7), fg="red",
                          command=make_remove(), relief=tk.FLAT).pack(side=tk.LEFT)

        # Add role dropdown
        add_frame = tk.Frame(title_inner)
        add_frame.grid(row=2, column=0, columnspan=10, sticky=tk.W, pady=(5, 0))

        tk.Label(add_frame, text="Add role:").pack(side=tk.LEFT)
        role_var = tk.StringVar()
        all_role_names = sorted(all_roles.keys())
        available = [r for r in all_role_names if r not in current_roles]
        if available:
            role_var.set(available[0])
            role_menu = tk.OptionMenu(add_frame, role_var, *available)
            role_menu.pack(side=tk.LEFT, padx=(2, 5))

            def add_title_mapping():
                r = role_var.get().strip()
                if not r:
                    return
                if window_title not in title_mappings:
                    title_mappings[window_title] = []
                if r not in title_mappings[window_title]:
                    title_mappings[window_title].append(r)
                save_title_mappings()
                rebuild_title_section()

            tk.Button(add_frame, text="Add", command=add_title_mapping).pack(side=tk.LEFT)
        else:
            tk.Label(add_frame, text="(all roles assigned)", font=("monospace", 9),
                     fg="gray").pack(side=tk.LEFT, padx=(5, 0))

        # New role creation
        new_frame = tk.Frame(title_inner)
        new_frame.grid(row=3, column=0, columnspan=10, sticky=tk.W, pady=(5, 0))
        tk.Label(new_frame, text="New role:").pack(side=tk.LEFT)
        new_role_entry = tk.Entry(new_frame, width=15)
        new_role_entry.pack(side=tk.LEFT, padx=(2, 5))

        def do_add_role():
            name = new_role_entry.get().strip()
            if not name or not all(c.isalnum() or c in '-_' for c in name):
                return
            if name in role_names:
                return
            sync_checkboxes_to_roles()
            role_names.append(name)
            roles[name] = {"mcpServers": {}, "permissions": {"allow": []}}
            rebuild_table()
            rebuild_title_section()

        tk.Button(new_frame, text="Create", command=do_add_role).pack(side=tk.LEFT)

    rebuild_title_section()

    # --- Scrollable table area ---
    table_outer = tk.Frame(root)
    table_outer.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

    canvas = tk.Canvas(table_outer)
    scrollbar_y = tk.Scrollbar(table_outer, orient=tk.VERTICAL, command=canvas.yview)
    scrollbar_x = tk.Scrollbar(table_outer, orient=tk.HORIZONTAL, command=canvas.xview)
    canvas.configure(yscrollcommand=scrollbar_y.set, xscrollcommand=scrollbar_x.set)

    scrollbar_y.pack(side=tk.RIGHT, fill=tk.Y)
    scrollbar_x.pack(side=tk.BOTTOM, fill=tk.X)
    canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    table_frame = tk.Frame(canvas)
    canvas_window = canvas.create_window((0, 0), window=table_frame, anchor=tk.NW)

    def on_frame_configure(event):
        canvas.configure(scrollregion=canvas.bbox("all"))

    def on_canvas_configure(event):
        canvas.itemconfig(canvas_window, width=event.width)

    table_frame.bind("<Configure>", on_frame_configure)
    canvas.bind("<Configure>", on_canvas_configure)

    # Mouse wheel scrolling
    def on_mousewheel(event):
        canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def on_mousewheel_linux(event):
        if event.num == 4:
            canvas.yview_scroll(-1, "units")
        elif event.num == 5:
            canvas.yview_scroll(1, "units")

    canvas.bind_all("<MouseWheel>", on_mousewheel)
    canvas.bind_all("<Button-4>", on_mousewheel_linux)
    canvas.bind_all("<Button-5>", on_mousewheel_linux)

    # Track checkbox vars: (rule, role_name) -> IntVar
    checkbox_vars = {}

    def rebuild_table():
        for widget in table_frame.winfo_children():
            widget.destroy()
        checkbox_vars.clear()

        # Header row
        tk.Label(table_frame, text="Source", font=("monospace", 9, "bold"),
                 anchor=tk.W, padx=5).grid(row=0, column=0, sticky=tk.W)
        tk.Label(table_frame, text="Rule", font=("monospace", 9, "bold"),
                 anchor=tk.W, padx=5).grid(row=0, column=1, sticky=tk.W)
        for col_idx, rname in enumerate(role_names):
            tk.Label(table_frame, text=rname, font=("monospace", 9, "bold"),
                     padx=5).grid(row=0, column=2 + col_idx)

        # Permission rows
        for row_idx, (rule, source, _path) in enumerate(permissions, start=1):
            bg = "#f0f0f0" if row_idx % 2 == 0 else "#ffffff"

            tk.Label(table_frame, text=source, font=("monospace", 9),
                     anchor=tk.W, padx=5, bg=bg).grid(row=row_idx, column=0, sticky=tk.W + tk.E)

            # Truncate long rules for display
            display_rule = rule if len(rule) <= 60 else rule[:57] + "..."
            lbl = tk.Label(table_frame, text=display_rule, font=("monospace", 9),
                           anchor=tk.W, padx=5, bg=bg)
            lbl.grid(row=row_idx, column=1, sticky=tk.W + tk.E)

            for col_idx, rname in enumerate(role_names):
                var = tk.IntVar()
                role_perms = roles.get(rname, {}).get("permissions", {}).get("allow", [])
                if rule in role_perms:
                    var.set(1)
                checkbox_vars[(rule, rname)] = var
                tk.Checkbutton(table_frame, variable=var, bg=bg).grid(
                    row=row_idx, column=2 + col_idx)

            # Remove button
            rm_idx = row_idx - 1  # capture for closure
            tk.Button(table_frame, text="x", font=("monospace", 7), fg="red",
                      command=lambda i=rm_idx: remove_permission(i), width=2,
                      relief=tk.FLAT, bg=bg).grid(row=row_idx, column=2 + len(role_names))

        # Make rule column expand
        table_frame.columnconfigure(1, weight=1)

    rebuild_table()

    # --- Bottom bar: save/close ---
    bottom_frame = tk.Frame(root)
    bottom_frame.pack(fill=tk.X, padx=10, pady=(5, 10))

    def save():
        # Update role data from checkboxes
        for rname in role_names:
            role_data = roles.get(rname, {"mcpServers": {}, "permissions": {"allow": []}})
            new_allow = []
            for rule, _source, _path in permissions:
                var = checkbox_vars.get((rule, rname))
                if var and var.get():
                    new_allow.append(rule)
            role_data.setdefault("permissions", {})["allow"] = new_allow
            roles[rname] = role_data

        save_roles(roles_dir, roles)
        status_label.config(text="Saved!", fg="green")
        root.after(2000, lambda: status_label.config(text=""))

    tk.Button(bottom_frame, text="Save", command=save, width=10).pack(side=tk.LEFT)
    tk.Button(bottom_frame, text="Close", command=root.destroy, width=10).pack(side=tk.RIGHT)
    status_label = tk.Label(bottom_frame, text="", fg="green")
    status_label.pack(side=tk.LEFT, padx=10)

    root.mainloop()


def main():
    if len(sys.argv) < 4:
        print("Usage: permissions_gui.py <session_config_dir> <cwd> <roles_dir> [config_dir]", file=sys.stderr)
        sys.exit(1)

    session_config_dir = sys.argv[1]
    cwd = sys.argv[2]
    roles_dir = Path(sys.argv[3])
    config_dir = sys.argv[4] if len(sys.argv) > 4 else None

    run_gui(session_config_dir, cwd, roles_dir, config_dir=config_dir)


if __name__ == "__main__":
    main()
