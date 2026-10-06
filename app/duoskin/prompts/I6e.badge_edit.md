---
id: I6e.badge_edit
version: 1
kind: image
provider: openai_images
builder: edit
route:
  draft: {model: gpt-image-2.5-flare-2026-09-08, quality: low, n: 4}
  final: {model: gpt-image-2.5-sunburst-2026-09-08, quality: high, n: 1}
size: 1024x1024
background: transparent
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
bootstrap: false
priming: [sticker, border, die-cut]
inputs:
  accessory: {kind: int}
  fix_sentence: {kind: str, required: true}
  keep: {kind: list}
  style_ref: {kind: flag}
flags: [style_ref]
checks:
  gate_a: [A_SIZE, A_ALPHA, A_COMPONENTS, A_MARGIN, A_OCR, A_GLYPH, A_PALETTE, A_STROKE, A_BADGE]
  gate_b: [ac_single_object, bd_compact_outline, ac_no_thin_parts, ac_matches_concept, ip_no_brand, ip_no_known_character, ip_no_text]
image1_role: edit_target
mask: all_editable
images:
- {id: current_asset, text: the current artwork.}
- {id: style_sheet, text: style sheet; match its outline weight and flat colouring only., when: style_ref}
---
PURPOSE: Small change to approved flat artwork for a small wearable item.
IMAGES: {images_line}
SUBJECT: The same artwork with one change.
MUST:
1. {fix_sentence}
2. Keep everything else exactly as in Image 1.
3. One compact centred shape with a smooth silhouette.
4. Flat front view, even outlines.
5. Large simple features.
STYLE: {style_block}
KEEP: {keep_list?}
OUTPUT: The artwork alone on a fully transparent background with clean hard alpha edges. Preserve the transparent background.
EXCLUDE: lettering, logos, white border, drop shadow, backdrop, holes, thin spikes, separate pieces, watermark.
