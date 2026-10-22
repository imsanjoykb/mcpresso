import matplotlib.pyplot as plt
import matplotlib.patches as patches
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch
import matplotlib.patheffects as path_effects

# Create architecture diagram
fig, ax = plt.subplots(1, 1, figsize=(14, 10))
ax.set_xlim(0, 14)
ax.set_ylim(0, 10)
ax.axis('off')

# Define colors
primary_color = '#2E4F4F'
secondary_color = '#0E8388'
accent_color = '#2E8B57'
text_color = '#333333'

# Title
title = ax.text(7, 9.5, 'MCPresso Pipeline Architecture', 
                ha='center', va='center', fontsize=18, fontweight='bold',
                color=primary_color)

# Input
input_box = FancyBboxPatch((0.5, 8.2), 3, 0.6, boxstyle="round,pad=0.1",
                          facecolor='lightblue', edgecolor=primary_color, linewidth=2)
ax.add_patch(input_box)
ax.text(2, 8.5, 'Natural Language\nDescription', ha='center', va='center', 
        fontsize=11, fontweight='bold')

# Registry Search
registry_box = FancyBboxPatch((0.5, 6.8), 3, 1, boxstyle="round,pad=0.1",
                            facecolor='lightgreen', edgecolor=secondary_color, linewidth=2)
ax.add_patch(registry_box)
ax.text(2, 7.3, 'Semantic Registry\nSearch\n(Similarity-based)', 
        ha='center', va='center', fontsize=10, fontweight='bold')

# Generation Modes
adapt_box = FancyBboxPatch((5, 7.5), 2.5, 0.8, boxstyle="round,pad=0.05",
                         facecolor='#FFE4E1', edgecolor='red', linewidth=1.5)
ax.add_patch(adapt_box)
ax.text(6.25, 7.9, 'ADAPT\n≥0.85 (~10s)', ha='center', va='center', fontsize=9)

seed_box = FancyBboxPatch((5, 6.5), 2.5, 0.8, boxstyle="round,pad=0.05",
                        facecolor='#FFF8DC', edgecolor='orange', linewidth=1.5)
ax.add_patch(seed_box)
ax.text(6.25, 6.9, 'SEED\n0.60-0.85 (~30s)', ha='center', va='center', fontsize=9)

full_box = FancyBboxPatch((5, 5.5), 2.5, 0.8, boxstyle="round,pad=0.05",
                        facecolor='#F0F8FF', edgecolor='blue', linewidth=1.5)
ax.add_patch(full_box)
ax.text(6.25, 5.9, 'FULL_GEN\n<0.60 (~60s)', ha='center', va='center', fontsize=9)

# LLM Generator
generator_box = FancyBboxPatch((8.5, 6), 4, 1.5, boxstyle="round,pad=0.1",
                             facecolor='lightyellow', edgecolor=accent_color, linewidth=2)
ax.add_patch(generator_box)
ax.text(10.5, 6.75, 'LLM Code Generator\n(Claude Sonnet 4.0)\n+ Template Integration', 
        ha='center', va='center', fontsize=11, fontweight='bold')

# Validator
validator_box = FancyBboxPatch((8.5, 4), 4, 1.5, boxstyle="round,pad=0.1",
                             facecolor='lightcoral', edgecolor='darkred', linewidth=2)
ax.add_patch(validator_box)
ax.text(10.5, 4.75, 'Five-Category Validator\nStructural • Protocol • Security\nRobustness • Documentation', 
        ha='center', va='center', fontsize=10, fontweight='bold')

# Repair Engine
repair_box = FancyBboxPatch((5, 2.5), 3, 1, boxstyle="round,pad=0.1",
                          facecolor='lightsteelblue', edgecolor='navy', linewidth=2)
ax.add_patch(repair_box)
ax.text(6.5, 3, 'Repair Engine\n(≤3 iterations)', ha='center', va='center', 
        fontsize=10, fontweight='bold')

# Scorer
scorer_box = FancyBboxPatch((8.5, 2), 4, 1, boxstyle="round,pad=0.1",
                          facecolor='lightseagreen', edgecolor='darkgreen', linewidth=2)
ax.add_patch(scorer_box)
ax.text(10.5, 2.5, 'Composite Scorer\nValidation 40% • Complexity 20%\nConsistency 20% • Security 20%', 
        ha='center', va='center', fontsize=10, fontweight='bold')

# Test Generator
test_box = FancyBboxPatch((0.5, 0.5), 3, 1, boxstyle="round,pad=0.1",
                        facecolor='thistle', edgecolor='purple', linewidth=2)
ax.add_patch(test_box)
ax.text(2, 1, 'Test Co-Generator\n3 tests per tool', ha='center', va='center', 
        fontsize=10, fontweight='bold')

# Output
output_box = FancyBboxPatch((8.5, 0.2), 4, 0.8, boxstyle="round,pad=0.1",
                          facecolor='gold', edgecolor='orange', linewidth=2)
ax.add_patch(output_box)
ax.text(10.5, 0.6, 'server.py + test_server.py + BrewResult', 
        ha='center', va='center', fontsize=11, fontweight='bold')

# Arrows
arrows = [
    # Main flow
    ((2, 8.2), (2, 7.8)),  # Input to Registry
    ((3.5, 7.3), (5, 6.9)),  # Registry to Modes
    ((7.5, 6.9), (8.5, 6.9)),  # Modes to Generator
    ((10.5, 6), (10.5, 5.5)),  # Generator to Validator
    ((8.5, 4.2), (7, 3.2)),  # Validator to Repair (conditional)
    ((6.5, 2.5), (10.2, 2.8)),  # Repair to Scorer
    ((10.5, 4), (10.5, 3)),  # Validator to Scorer (direct)
    ((10.5, 2), (10.5, 1)),  # Scorer to Output
    ((8.5, 0.6), (3.5, 1)),  # Output to Test Gen
    
    # Repair loop
    ((5.5, 3.5), (8.5, 4.5)),  # Repair back to Validator
]

for start, end in arrows:
    arrow = FancyArrowPatch(start, end, arrowstyle='->', mutation_scale=20,
                          color=primary_color, linewidth=2)
    ax.add_patch(arrow)

# Decision diamond for repair
diamond = patches.RegularPolygon((6.5, 4.2), 4, radius=0.3, orientation=45,
                               facecolor='yellow', edgecolor='black', linewidth=2)
ax.add_patch(diamond)
ax.text(6.5, 4.2, 'Score\n<75?', ha='center', va='center', fontsize=8, fontweight='bold')

# Legend
legend_elements = [
    patches.Rectangle((0, 0), 1, 1, facecolor='lightblue', edgecolor=primary_color, label='Input/Output'),
    patches.Rectangle((0, 0), 1, 1, facecolor='lightgreen', edgecolor=secondary_color, label='Memory/Storage'),
    patches.Rectangle((0, 0), 1, 1, facecolor='lightyellow', edgecolor=accent_color, label='Generation'),
    patches.Rectangle((0, 0), 1, 1, facecolor='lightcoral', edgecolor='darkred', label='Validation'),
    patches.Rectangle((0, 0), 1, 1, facecolor='lightsteelblue', edgecolor='navy', label='Repair'),
]

# Add performance indicators
ax.text(1, 9, '🚀 Target: <60s end-to-end', fontsize=10, fontweight='bold', color='green')
ax.text(1, 8.7, '✅ Success Rate: 78%', fontsize=10, fontweight='bold', color='blue')
ax.text(1, 8.4, '🔧 Repair Convergence: 85%', fontsize=10, fontweight='bold', color='orange')

plt.tight_layout()
plt.savefig('architecture_diagram.pdf', dpi=300, bbox_inches='tight', facecolor='white')
plt.savefig('architecture_diagram.svg', bbox_inches='tight', facecolor='white')
plt.close()

print("Architecture diagram generated successfully!")