#!/bin/bash -e

# PowerShell unlocking
powershell Set-ExecutionPolicy -ExecutionPolicy Unrestricted -Scope LocalMachine

# Install required stuff
powershell ".\\tools\\windows\\deploy_ci_agent.ps1" -install_release

# Test the auto-updater
rm -rf build dist
powershell ".\\tools\\windows\\deploy_ci_agent.ps1" -check_upgrade