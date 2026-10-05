---
id: I5g.accessory_guided
version: 1
kind: image
provider: openai_images
builder: i5
route:
  draft: {model: gpt-image-2.5-flare-2026-09-08, quality: low, n: 4}
  final: {model: gpt-image-2.5-sunburst-2026-09-08, quality: high, n: 1}
size: 1024x1024
background: transparent
must_lines: 5
dna_fields: [motif_object, shape_language]
slots:
  item_noun: {source: phrases.item_noun}
  accessory_description:
    source: accessory.description
    max_words: 15
    lint: [free_text]
  material_phrase: {source: phrases.material_phrase}
  colour_names: {source: accessory.colour_refs -> colour_names.json}
  attachment_option: {source: phrases.attachment_option}
  motif_object:
    source: dna.motif_object
    max_words: 5
    lint: [free_text]
  shape_short: {source: dna.shape_language}
style_block: HOUSE_STYLE_3D_INPUT
bootstrap: false
priming: [character, avatar, hand, shelf]
inputs:
  accessory: {kind: int, required: true}
  no_crop: {kind: flag}
flags: [no_crop]
checks:
  gate_a: [A_SIZE, A_ALPHA, A_COMPONENTS, A_MARGIN, A_STROKE, A_SYMMETRY, A_OCR, A_GLYPH, A_PALETTE]
  gate_b: [ac_single_object, ac_front_ortho, ac_no_thin_parts, ac_matches_concept, ac_flat_light, ip_no_brand, ip_no_known_character,
    ip_no_text]
image1_role: edit_target
mask: guide_box_dilated_4pct
images:
- {id: guide_acc_box, text: grey box showing the size and position the whole object must fill.}
- {id: ref_crop, text: 'design reference; recreate this {item_noun} as a standalone object.', when: '!no_crop'}
- {id: style_sheet, text: style sheet; match its colouring only.}
---
PURPOSE: Reference image of one small toy-like object; the single input image for 3D model generation.
IMAGES: {images_line}
SUBJECT: {accessory_description}; {material_phrase}; colours {colour_names}.
MUST:
1. Fill the grey box with the whole object, centred and symmetric left to right, seen straight from the front (orthographic), nothing outside the box.
2. One solid connected object with thick simple parts[[; {attachment_option}]].
3. Soft even light from the front and slightly above, matte surfaces, true flat base colours with one soft shadow step.
4. The object's main form echoes this motif: {motif_object}; the object itself stays as described.
5. Surface details are {shape_short}; the whole object stays one solid piece with thick parts.
STYLE: {style_block}
OUTPUT: The object alone on a fully transparent background with clean hard alpha edges.
EXCLUDE: floor, plinth, cast shadow, hands, people, packaging, text, logos, watermark.
