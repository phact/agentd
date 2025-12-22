#!/bin/bash
# Install microsandbox server for agentd
# https://github.com/microsandbox/microsandbox (Apache 2.0)

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo "Checking microsandbox installation..."

# Check if already installed
if command -v msb &> /dev/null; then
    echo -e "${GREEN}microsandbox already installed:${NC} $(msb --version 2>/dev/null || echo 'version unknown')"
    exit 0
fi

# Check platform requirements
if [[ "$OSTYPE" == "linux-gnu"* ]]; then
    if [[ ! -e /dev/kvm ]]; then
        echo -e "${RED}Error: KVM not available.${NC}"
        echo "Enable virtualization in BIOS/UEFI and ensure kvm module is loaded:"
        echo "  sudo modprobe kvm"
        echo "  sudo modprobe kvm_intel  # or kvm_amd"
        exit 1
    fi
    echo -e "${GREEN}KVM available${NC}"

elif [[ "$OSTYPE" == "darwin"* ]]; then
    if [[ $(uname -m) != "arm64" ]]; then
        echo -e "${RED}Error: Requires Apple Silicon (M1/M2/M3/M4)${NC}"
        echo "microsandbox uses Hypervisor.framework which requires ARM64."
        exit 1
    fi
    echo -e "${GREEN}Apple Silicon detected${NC}"

elif [[ "$OSTYPE" == "msys"* ]] || [[ "$OSTYPE" == "cygwin"* ]] || [[ "$OSTYPE" == "win32"* ]]; then
    echo -e "${RED}Error: Windows not yet supported${NC}"
    echo "See https://github.com/microsandbox/microsandbox for updates."
    exit 1

else
    echo -e "${YELLOW}Warning: Unknown platform '$OSTYPE', attempting install anyway...${NC}"
fi

# Install microsandbox
echo "Installing microsandbox..."
curl -sSL https://get.microsandbox.dev | sh

# Verify installation
if command -v msb &> /dev/null; then
    echo -e "${GREEN}microsandbox installed successfully!${NC}"
    msb --version 2>/dev/null || true
else
    # Check common install locations
    if [[ -f "$HOME/.local/bin/msb" ]]; then
        echo -e "${YELLOW}Installed to ~/.local/bin/msb${NC}"
        echo "Add to PATH: export PATH=\"\$HOME/.local/bin:\$PATH\""
    elif [[ -f "/usr/local/bin/msb" ]]; then
        echo -e "${GREEN}Installed to /usr/local/bin/msb${NC}"
    else
        echo -e "${RED}Installation may have failed. Check output above.${NC}"
        exit 1
    fi
fi

echo ""
echo "To start the server:"
echo "  msb server start --dev"
echo ""
echo "Or run in background:"
echo "  msb server start --dev &"
