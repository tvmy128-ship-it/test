---
id: I3e.face_part_edit
version: 1
kind: image
provider: openai_images
builder: edit
route:
  draft: {model: gpt-image-2.5-flare-2026-09-08, quality: low, n: 4}
  final: {model: gpt-image-2.5-sunburst-2026-09-08, quality: high, n: 1}
size: null
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
  orientation_rule: {source: phrases.r1.orientation_rule}
  exclude_list: {source: phrases.i3.exclude_base}
style_block: none
bootstrap: false
priming: [face, head, skin, both eyes]
inputs:
  part:
    kind: str
    required: true
    values: [iris, lash_upper, brow, mouth_closed, mouth_open]
  fix_sentence: {kind: str, required: true}
  keep: {kind: list}
  style_ref: {kind: flag}
flags: [style_ref]
checks:
  gate_a: [A_SIZE, A_ALPHA, A_GUIDE_LEFT, A_SIL_GUIDE, A_SINGLE_COLOUR, A_HIGHLIGHT, A_STROKE, A_OCR, A_PHASH]
  gate_b: [fp_single_feature, fp_front_view, fp_shape_word, fp_orientation, fp_one_line_colour, fp_no_highlight, fp_thick_shapes, fp_style_match]
image1_role: edit_target
mask: all_editable
images:
- {id: current_asset, text: the current drawing element.}
- {id: style_sheet, text: style sheet; match its line weight and colouring., when: style_ref}
---
PURPOSE: Small change to an approved flat drawing element for a character texture.
IMAGES: {images_line}
SUBJECT: The same drawing element with one change.
MUST:
1. {fix_sentence}
2. Keep everything else exactly as in Image 1.
3. Cover the same shape exactly: same position, size and outline.
4. Clean vector-like shapes, crisp edges, flat fills.
5. {orientation_rule}
KEEP: {keep_list?}
OUTPUT: The element alone on a fully transparent background with clean hard alpha edges. Preserve the transparent background.
EXCLUDE: skin, head, second eye, eyeshadow, highlight dots, shadow, text, watermark.
