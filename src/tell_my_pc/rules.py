"""Built-in Never rules. Config may add to these; it can only switch them off as a whole
(``policy.builtin_never: false``) and then must supply its own, or the config is refused."""

from __future__ import annotations

import os

# Programs that are never run, whatever the tier or allowlist says. Nested shells and
# interpreters are here because they would bypass command parsing.
NEVER_PROGRAMS: frozenset[str] = frozenset(
    {
        # disk, boot, registry, system
        "format", "diskpart", "fdisk", "mkfs", "dd", "parted", "bcdedit", "bootcfg",
        "reg", "regedit", "regini", "sfc", "dism", "vssadmin", "wbadmin", "cipher",
        "takeown", "icacls", "cacls", "attrib", "shutdown", "restart-computer",
        "stop-computer", "sudo", "su", "doas", "runas", "chown", "chmod", "mount",
        "umount", "launchctl", "systemctl", "csrutil", "diskutil", "schtasks", "sc", "wmic",
        "netsh", "setx", "set-itemproperty", "new-itemproperty", "remove-itemproperty",
        # nested shells / interpreters that defeat command parsing
        "cmd", "powershell", "pwsh", "bash", "sh", "zsh", "fish", "wsl", "env", "xargs",
        "eval", "exec", "invoke-expression", "iex", "start-process", "nohup", "osascript",
        # credential helpers
        "security", "cmdkey", "vaultcmd", "git-credential", "gh",
    }
)  # fmt: skip

# Path globs. "~" is the user's home. A pattern without "/" matches the file name anywhere.
NEVER_PATHS: tuple[str, ...] = (
    # Claude's own login and settings: never read, copied or forwarded
    "~/.claude/**",
    "~/.claude.json",
    "~/.claude.json.*",
    "~/.config/claude/**",
    # common credential stores
    "~/.ssh/**",
    "~/.aws/**",
    "~/.azure/**",
    "~/.gnupg/**",
    "~/.kube/**",
    "~/.docker/config.json",
    "~/.config/gh/**",
    "~/.config/gcloud/**",
    "~/.netrc",
    "~/_netrc",
    "~/.git-credentials",
    "~/.npmrc",
    "~/.pypirc",
    "~/AppData/Roaming/Microsoft/Credentials/**",
    "~/AppData/Local/Microsoft/Credentials/**",
    "~/Library/Keychains/**",
    # system folders
    "C:/Windows/**",
    "C:/Program Files/**",
    "C:/Program Files (x86)/**",
    "C:/ProgramData/**",
    "/etc/**",
    "/usr/**",
    "/bin/**",
    "/sbin/**",
    "/boot/**",
    "/System/**",
    "/Library/**",
    "/private/etc/**",
    # secret-looking files anywhere
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.pfx",
    "*.p12",
    "*.kdbx",
    "id_rsa*",
    "id_ecdsa*",
    "id_ed25519*",
    ".credentials.json",
    "credentials.json",
    ".git-credentials",
)


def program_name(arg0: str) -> str:
    """Normalise a program token: basename, lower case, no .exe/.cmd/.bat/.ps1 suffix."""
    name = os.path.basename(arg0.replace("\\", "/")).lower()
    for suffix in (".exe", ".cmd", ".bat", ".ps1", ".com"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return name
