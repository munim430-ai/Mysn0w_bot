#!/bin/bash
# Setup script for Bangladesh RMG Factory Intelligence Pipeline

set -e

echo "========================================"
echo "RMG Factory Pipeline Setup"
echo "========================================"

# Check Python version
python_version=$(python3 --version 2>&1 | awk '{print $2}')
echo "Python version: $python_version"

# Install dependencies
echo "Installing Python dependencies..."
pip install -r requirements.txt

# Install Playwright browsers
echo "Installing Playwright Chromium browser..."
playwright install chromium

# Setup .env file
if [ ! -f .env ]; then
    echo "Creating .env file..."
    cp .env.example .env
    echo ""
    echo "IMPORTANT: Edit .env and add your GitHub PAT:"
    echo "  GH_PAT=ghp_your_token_here"
    echo ""
    echo "Get your PAT from: https://github.com/settings/tokens"
fi

# Create factory-data branch if pushing manually
echo ""
echo "Setup complete!"
echo ""
echo "Next steps:"
echo "  1. Edit .env with your GitHub PAT"
echo "  2. Run: python src/run.py --dry-run  (to test without uploading)"
echo "  3. Run: python src/run.py            (full pipeline)"
echo ""
