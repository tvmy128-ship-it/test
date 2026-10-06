---
id: I2e.print_edit
version: 1
kind: image
provider: openai_images
builder: edit
route:
  draft: {model: gpt-image-2.5-flare-2026-09-08, quality: low, n: 4}
  final: {model: gpt-image-2.5-sunburst-2026-09-08, quality: high, n: 1}
size: {square: 1024x1024, tall: 816x1632}
background: transparent
image1_role: edit_target
mask: all_editable
must_lines: 5
dna_fields: []
slots:
  fix_sentence:
    source: L7 fix_sentence
    max_words: 25
    lint: [free_text]
  keep_list:
    source: L7 keep
    lint: [free_text]
style_block: HOUSE_STYLE_2D
priming: [shirt, t-shirt, garment, mockup, avatar]
bootstrap: false
checks:
  gate_a: [A_SIZE, A_ALPHA, A_COMPONENTS, A_MARGIN, A_OCR, A_GLYPH, A_PALETTE, A_STROKE, A_HALO, A_PHASH]
  gate_b: [pr_single_graphic, pr_motif_matches, pr_flat_front, pr_readable_small, ip_no_brand, ip_no_known_character, ip_no_text, pr_matches_concept]
images:
- {id: current_asset, text: the current graphic.}
- {id: style_sheet, text: style sheet; match its outline weight and flat colouring only., when: style_ref}
inputs:
  fix_sentence: {kind: str, required: true}
  keep: {kind: list}
  style_ref: {kind: flag}
flags: [style_ref]
---
PURPOSE: Small change to an approved flat graphic.
IMAGES: {images_line}
SUBJECT: The same graphic with one change.
MUST:
1. {fix_sentence}
2. Keep everything else exactly as in Image 1.
3. One centred graphic, the whole design visible, margin kept.
4. Bold simple shapes with even outlines.
5. Straight-on flat artwork with flat fills and no perspective.
STYLE: {style_block}
KEEP: {keep_list?}
OUTPUT: The graphic alone on a fully transparent background with clean hard alpha edges. Preserve the transparent background.
EXCLUDE: lettering, numbers, logos, garment, mockup, rectangle backdrop, drop shadow, gradients, watermark.
