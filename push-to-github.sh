#!/bin/bash
# Push pipeline code to GitHub factory-data branch

set -e

REPO_URL="https://github.com/munim430-ai/Mysn0w_bot.git"

echo "========================================"
echo "Pushing to GitHub: $REPO_URL"
echo "========================================"

# Check for PAT
if [ -z "$GH_PAT" ]; then
    echo "ERROR: GH_PAT environment variable not set"
    echo ""
    echo "Set it with:"
    echo "  export GH_PAT=ghp_your_token_here"
    echo ""
    echo "Or run with:"
    echo "  GH_PAT=ghp_xxx ./push-to-github.sh"
    exit 1
fi

# Configure remote with PAT
REMOTE_WITH_AUTH="https://${GH_PAT}@github.com/munim430-ai/Mysn0w_bot.git"

echo "Configuring authenticated remote..."
git remote remove origin 2>/dev/null || true
git remote add origin "$REMOTE_WITH_AUTH"

# Push master branch
echo "Pushing master branch..."
git push -u origin master

# Create and push factory-data branch
echo "Creating factory-data branch..."
git branch factory-data 2>/dev/null || true
git checkout factory-data
git push -u origin factory-data

echo ""
echo "========================================"
echo "Successfully pushed to GitHub!"
echo "========================================"
echo ""
echo "Branches:"
echo "  - master: pipeline code"
echo "  - factory-data: scraped data outputs"
echo ""
echo "Run the pipeline:"
echo "  python src/run.py"
