#!/bin/bash

# MCPresso Research Paper Compilation Script
# Creates camera-ready PDF and submission package

echo "🔧 Compiling MCPresso Research Paper..."

# Check if pdflatex is available
if ! command -v pdflatex &> /dev/null; then
    echo "❌ pdflatex not found. Please install a LaTeX distribution (e.g., MacTeX, TeX Live)"
    echo "   You can install MacTeX from: https://www.tug.org/mactex/"
    exit 1
fi

# Compile the paper
echo "📄 Compiling LaTeX document..."
pdflatex -interaction=nonstopmode mcpresso_research_paper.tex > /dev/null 2>&1
bibtex mcpresso_research_paper > /dev/null 2>&1
pdflatex -interaction=nonstopmode mcpresso_research_paper.tex > /dev/null 2>&1
pdflatex -interaction=nonstopmode mcpresso_research_paper.tex > /dev/null 2>&1

if [ -f "mcpresso_research_paper.pdf" ]; then
    echo "✅ Paper compilation successful!"
else
    echo "❌ Paper compilation failed. Check LaTeX errors:"
    pdflatex mcpresso_research_paper.tex
    exit 1
fi

# Create submission package
echo "📦 Creating submission package..."
mkdir -p submission_package
mkdir -p submission_package/figures
mkdir -p submission_package/supplementary

# Copy main files
cp mcpresso_research_paper.tex submission_package/
cp references.bib submission_package/
cp mcpresso_research_paper.pdf submission_package/

# Copy figures
cp *.pdf submission_package/figures/ 2>/dev/null || echo "Note: Some PDFs may be the main paper"
cp architecture_diagram.svg submission_package/figures/

# Copy supplementary materials
cp supplementary_material.md submission_package/supplementary/
cp conference_submission_guide.md submission_package/supplementary/
cp literature_review_*.md submission_package/supplementary/

# Create README for submission
cat > submission_package/README.md << 'EOF'
# MCPresso Research Paper - Submission Package

## Main Files
- `mcpresso_research_paper.tex` - Main LaTeX source
- `mcpresso_research_paper.pdf` - Compiled paper
- `references.bib` - Bibliography

## Figures
- `architecture_diagram.pdf/svg` - System architecture
- `performance_by_category.pdf` - Performance analysis
- `ablation_study.pdf` - Ablation study results
- `registry_performance.pdf` - Registry effectiveness
- `quality_distribution.pdf` - Quality score analysis
- `readiness_distribution.pdf` - Readiness tiers

## Supplementary Materials
- `supplementary_material.md` - Extended results and analysis
- `conference_submission_guide.md` - Venue-specific guidance
- `literature_review_*.md` - Related work analysis

## Compilation Instructions
```bash
pdflatex mcpresso_research_paper.tex
bibtex mcpresso_research_paper
pdflatex mcpresso_research_paper.tex
pdflatex mcpresso_research_paper.tex
```

## Paper Statistics
- Main paper: ~20 pages (conference version will be compressed to venue limits)
- Figures: 6 high-quality visualizations
- References: 25+ recent citations
- Supplementary: Comprehensive experimental details
EOF

# Create archive
echo "🗜️  Creating submission archive..."
cd submission_package
tar -czf ../mcpresso_paper_submission.tar.gz *
cd ..

# Generate paper statistics
echo "📊 Paper Statistics:"
echo "==================="
echo "Main paper size: $(wc -l < mcpresso_research_paper.tex) lines"
echo "Bibliography entries: $(grep -c '^@' references.bib)"
echo "Figure count: $(ls -1 *.pdf | wc -l | xargs)"
echo "Supplementary material: $(wc -l < supplementary_material.md) lines"
echo ""

# Final checklist
echo "✅ Submission Package Complete!"
echo "================================"
echo "📁 submission_package/ - Contains all submission files"
echo "📦 mcpresso_paper_submission.tar.gz - Ready for upload"
echo "📄 mcpresso_research_paper.pdf - Final paper ($(du -h mcpresso_research_paper.pdf | cut -f1))"
echo ""
echo "🎯 Recommended next steps:"
echo "1. Review the compiled PDF for formatting issues"
echo "2. Check figure quality and readability"  
echo "3. Verify all references are properly formatted"
echo "4. Run plagiarism check if required"
echo "5. Submit to target conference"
echo ""
echo "🚀 Ready for submission to top-tier venues!"

# Cleanup temporary files
rm -f *.aux *.log *.bbl *.blg *.out *.toc *.lof *.lot