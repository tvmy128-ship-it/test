"""2D imaging utilities: unicode-safe I/O, masks, guides, Gate A code checks, SVG, OCR, similarity, face canvas.

Heavy native modules (cv2, scipy, onnxruntime, rapidocr, resvg_py) are imported lazily inside the functions that need
them, so importing this package is cheap.
"""
