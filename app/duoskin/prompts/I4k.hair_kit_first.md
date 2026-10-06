---
id: I4k.hair_kit_first
version: 1
kind: image
provider: openai_images
builder: i4
route:
  draft: {model: gpt-image-2.5-flare-2026-09-08, quality: low, n: 4}
  final: {model: gpt-image-2.5-sunburst-2026-09-08, quality: high, n: 1}
  ab_final: {model: gpt-image-2.5-sunburst-2026-09-08, quality: xhigh, n: 1}
size: 1024x1536
background: opaque
image1_role: edit_target
mask: hair_box_minus_lower_55pct_of_head_front
must_lines: 5
dna_fields: [shape_language]
slots:
  hair_phrase:
    source: kit phrase + hair.description
    max_words: 16
    lint: [free_text]
  parting_clause: {source: phrases.hair}
  k: {source: kit clump_k}
  fringe_line: {source: phrases.hair}
  shape_language_line: {source: dna.shape_language}
style_block: HOUSE_STYLE_3D_INPUT
priming: [face, eyes, hat]
inputs: {}
flags: []
checks:
  gate_a: [A_SIZE, A_SIL_GUIDE, A_SYMMETRY, A_OCR]
  gate_b: [hr_head_unchanged, hr_no_face, hr_front_ortho, hr_bangs_clear, hr_matches_concept, hr_chunky, hr_volume_readable, ip_no_brand,
    ip_no_known_character, ip_no_text]
bootstrap: false
images:
- {id: guide_bald_head_with_kit_hair, text: 'grey cube head, front view, wearing a base hairstyle with the right clump structure.'}
- {id: approved_hair, text: 'approved hairstyle, front and back; take its shape, parting, length and colours, not its drawing style.'}
notes: 'I4 ladder rung: kit render first, as the edited image. Skipped for hair_custom and while the hair kit is empty.'
---
PURPOSE: Front-view hairstyle design used as the input for 3D modelling.
IMAGES: {images_line}
SUBJECT: Reshape the hair in Image 1 into Image 2's hairstyle: {hair_phrase}.
MUST:
1. Keep the grey head's exact size, shape and position; the hair wraps it like a wig.
2. Straight-on front view, level, no perspective{parting_clause}.
3. Keep Image 1's large chunky sculpted clumps and volume, about {k} of them, and change only what is needed to match Image 2's shape and length.
4. {fringe_line}
5. {shape_language_line}
STYLE: {style_block}
KEEP: the white background and the bare lower part of the head unchanged.
EXCLUDE: facial features, hat, hair accessories, body, text, watermark.
