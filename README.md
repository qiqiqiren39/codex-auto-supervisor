# Codex Auto Supervisor

[English](README.md) | [简体中文](README.zh-CN.md)

An external, configuration-free automatic supervisor for Codex threads. It watches an explicitly registered target thread and wakes a separate coordinator thread when the target completes, fails, asks a question, or requests approval.

## Install from a Release (Recommended)

Latest release:

https://github.com/qiqiqiren39/codex-auto-supervisor/releases/latest

1. Open the Releases page and download the latest ZIP, for example codex-auto-supervisor-v1.0.0.zip.
2. Extract the ZIP. You should get one complete folder named codex-auto-supervisor.
3. Copy the entire codex-auto-supervisor folder into your Codex skills directory:

       %USERPROFILE%\.codex\skills\

   If CODEX_HOME is set, use:

       %CODEX_HOME%\skills\

4. Confirm the installed path looks like:

       %USERPROFILE%\.codex\skills\codex-auto-supervisor\SKILL.md

   Copy the whole folder. Do not copy only SKILL.md or individual files from scripts.

5. Restart the Codex desktop app or reload skills.
6. In a chat, explicitly ask it to monitor a thread:

       Use $codex-auto-supervisor to watch the target thread and wake the coordinator thread when it finishes.

   The agent will confirm the target and coordinator thread IDs, register the target, and start the background watcher. If you already know the UUIDs, provide them directly.
7. Check monitoring status:

       threadwatch status

   If threadwatch is not on PATH:

       py -3 <skill-directory>\scripts\threadwatch.py status

No administrator rights, pip install, config.toml edit, or login startup entry is required.

## Requirements

- Windows 10 or later
- Python 3.11 or later
- A signed-in Codex desktop app or a compatible Codex CLI app-server

Check Python with:

    py -3 --version

## Usage

The recommended flow is to let Codex create or choose a coordinator thread, then register the target. Example request:

    Use $codex-auto-supervisor to watch this target thread. Wake the coordinator when it completes, fails, asks a question, or requests approval.

The agent performs the equivalent of:

    threadwatch add --target <target-uuid> --wake <coordinator-uuid> --project <absolute-project-path>
    threadwatch start
    threadwatch status

The default start point is the end of the target log when it is registered. To replay the most recent terminal event once, add --replay-last.

On completion, failure, HTTP 429, approval, or an async question, the watcher queues an event to the coordinator with the local codex queue command. A coordinator message beginning with [threadwatch] is an external event, not a new user instruction. The coordinator inspects the target and decides the next action.

## Stop and uninstall

Stop the daemon while keeping registrations:

    threadwatch stop

Remove one target:

    threadwatch remove --target <target-uuid>

Inspect diagnostics and logs:

    threadwatch doctor
    threadwatch logs --follow

Full uninstall:

1. Stop the daemon and remove targets.
2. Delete the codex-auto-supervisor folder from the skills directory.
3. To remove local state as well, run:

       powershell.exe -NoProfile -ExecutionPolicy Bypass -File <skill-directory>\scripts\uninstall.ps1 -PurgeState

State is stored in %LOCALAPPDATA%\CodexAutoSupervisor and contains only target UUIDs, offsets, event keys, and logs.

## Troubleshooting

### The skill does not appear in Codex

Confirm that ...\skills\codex-auto-supervisor\SKILL.md exists and that there is not an extra nesting level from ZIP extraction. Restart Codex after moving the folder.

### py or Python is not found

Install Python 3.11 or newer and make sure py -3 --version works. If it does not, ask the agent to invoke scripts\threadwatch.py with the absolute path of an existing Python installation.

### threadwatch is not on PATH

The CLI does not need to be on PATH. Run:

    py -3 <skill-directory>\scripts\threadwatch.py <command>

To install an optional launcher:

    powershell.exe -NoProfile -ExecutionPolicy Bypass -File <skill-directory>\scripts\install.ps1

### No event arrives

Run threadwatch status --json and threadwatch doctor. Verify that the target is registered, its session log was found, and the daemon is running. The target must produce a new task_complete, question, or approval event. Historical events are not replayed by default.

## Why this project

Most Codex monitoring tools cover one part of the loop: showing status, playing a completion sound, recovering a quota limit in the same thread, or watching an arbitrary shell command. Codex Auto Supervisor focuses on the handoff boundary between two Codex threads:

- It reads appended session events without changing Codex configuration, installing hooks, or registering a plugin.
- The watched target and coordinator are explicit. Other threads and projects are ignored.
- Completion, failure, HTTP 429, approval requests, and async questions can all become coordinator events.
- The event wakes a separate coordinator thread through the existing local codex queue command, so the coordinator can decide the next action rather than only notifying a person.
- Idle checks read local file metadata only. They do not call a model or spend API tokens.
- The same package is both a Codex skill and a standalone CLI, with manual start and stop and no login startup entry.
- Byte offsets and event keys are persisted, so a daemon restart continues from the saved position and does not replay the same event.

## Developer source install

For development, clone the repository into your skills directory:

    git clone https://github.com/qiqiqiren39/codex-auto-supervisor.git
    py -3 -m unittest discover -s tests -v

The source repository contains tests and release-build tooling. The Release ZIP contains only the files needed by consumers.
