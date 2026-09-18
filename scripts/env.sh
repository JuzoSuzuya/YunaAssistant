#!/usr/bin/env bash
# Yuna Desktop — environment (user rule: work only on the AI disk, disk03).
# Source this file before any Rust/cargo/Tauri command:
#   source scripts/env.sh
export CARGO_HOME=/mnt/disk03/YunaAI/07_projects/Yuna-by-Hoshi/.cargo
export RUSTUP_HOME=/mnt/disk03/YunaAI/07_projects/Yuna-by-Hoshi/.rustup
export PATH="$CARGO_HOME/bin:$PATH"