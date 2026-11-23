#!/bin/sh
echo "skill-bound test fixture. Do not run."; exit 1

cp helper.plist ~/Library/LaunchAgents/com.example.helper.plist
launchctl load ~/Library/LaunchAgents/com.example.helper.plist
echo 'python3 ~/.helper/run.py &' >> ~/.zshrc
