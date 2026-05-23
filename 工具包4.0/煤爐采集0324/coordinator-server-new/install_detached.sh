#!/bin/bash
# Install v2 dependencies in detached mode so SSH can disconnect immediately.
# Run output to /tmp/v2_install.log, status in /tmp/v2_install.done
set -e

LOG=/tmp/v2_install.log
DONE=/tmp/v2_install.done

# Use Chinese npm mirror to speed up from Aliyun China
NPM_REGISTRY=https://registry.npmmirror.com

rm -f "$DONE"
echo "=== START $(date) ===" > "$LOG"

cd /root/coordinator-server-v2

# Run npm with mirror, detached
nohup bash -c "
    npm config set registry $NPM_REGISTRY 2>&1
    npm install --no-audit --no-fund --omit=dev 2>&1
    EC=\$?
    echo \"EXIT_CODE=\$EC\"
    if [ \$EC -eq 0 ]; then
        echo 'SUCCESS' > $DONE
    else
        echo 'FAILED' > $DONE
    fi
    echo \"=== END \$(date) ===\"
" >> "$LOG" 2>&1 &

echo "started, pid=$!"
echo "tail $LOG to see progress; $DONE indicates completion"
