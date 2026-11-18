# Firejail profile for MCP servers - restrictive sandbox
# Usage: firejail --profile=mcp-restrictive.profile python mcp_server.py

# Basic restrictions
include /etc/firejail/disable-common.inc
include /etc/firejail/disable-programs.inc
include /etc/firejail/disable-devel.inc

# Network - no network access by default
net none

# Filesystem restrictions
noroot
noexec ${HOME}
noexec /tmp
noexec /var/tmp

# Whitelist only necessary paths
# Workspace directory (will be substituted at runtime)
whitelist ${WORKSPACE}

# Temp directory for MCP server
whitelist ${TEMP}

# Python and standard library
whitelist /usr/bin/python*
whitelist /usr/lib/python*
whitelist /usr/local/lib/python*

# Read-only system directories
ro-bind /usr /usr
ro-bind /lib /lib
ro-bind /lib64 /lib64
ro-bind /bin /bin
ro-bind /sbin /sbin
ro-bind /etc /etc

# Memory and CPU limits (enforced by firejail)
rlimit-as 536870912  # 512MB virtual memory
rlimit-cpu 300       # 5 minutes CPU time
rlimit-fsize 10485760 # 10MB file size
rlimit-nofile 64     # max 64 open files
rlimit-nproc 32      # max 32 processes

# Drop capabilities
caps.drop all

# Seccomp filter - restrict syscalls
seccomp

# Protocol handlers - none
protocol none

# Disable dangerous features
no3d
nou2f
noautopulse
nodbus
nogpu
nomidi
nonewprivs
noroot
nosound
novideo