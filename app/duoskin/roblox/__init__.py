"""roblox package (DuoSkin Studio): Roblox facts and rules.

Clothing side (this track): ``template`` (the classic 585x559 Shirt/Pants template: regions, crops, seams, gap fill, bleed, flat and
box previews), ``validators`` (``validate_template``; also re-exports ``validate_accessory``), ``limits_clothing`` and ``checklist``
(the upload checklist built from the item-type table), ``fees`` (upload fees, publishing advances and creator requirements, every figure labelled "check Roblox for current prices"). Accessory side: ``limits`` and ``mesh_validators``.

Nothing is imported here on purpose: importing the package stays cheap and free of numpy/Pillow work.
"""
