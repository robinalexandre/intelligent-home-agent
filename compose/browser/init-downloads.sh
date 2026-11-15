#!/bin/sh
# Run by the linuxserver image at start: Chrome (user abc) saves the agent's downloads here.
mkdir -p /downloads
chown abc:abc /downloads
