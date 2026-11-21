#!/bin/sh
set -eu
cd "$(dirname "$0")"
ME="$(id -un)"                                   # Dienst läuft als dieser Benutzer, nicht als root
mkdir -p data
sudo chown -R "$ME": data 2>/dev/null || true
sed -e "s|@ROOT@|$(pwd)|g" -e "s|@USER@|$ME|g" deploy/kapbot.service | sudo tee /etc/systemd/system/kapbot.service >/dev/null
sudo cp deploy/kapbot.timer /etc/systemd/system/kapbot.timer
sudo systemctl daemon-reload
sudo systemctl enable --now kapbot.timer
