#!/bin/bash
# Move to the Aura project directory regardless of where the script is called from
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd "$DIR"

# Launch Aura using the project environment
if [ -f "./venv/bin/python" ]; then
    ./venv/bin/python start_aura.py
else
    python3 start_aura.py
fi
