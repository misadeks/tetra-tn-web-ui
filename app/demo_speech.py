"""Precomputed synthetic speech for the fake stack (demo/testing only).

A short looping ACELP-encoded two-tone pattern so the downlink-voice path can
be exercised end to end without real radio hardware. Each entry is base64 of
35 bytes = 274 bits (MSB-first) of one MsSpeechFrame (two 137-bit sub-frames).
"""
from __future__ import annotations

import base64

_PACKED_FRAMES = [
    "GrBSXO67+0sQBkgMRbi6kQcNVikyw+QR8bwdrc7IRbVIh8A=",
    "GrHSaDgGm4QG41AOAYXjwA+P1ik0HuFQQlA1p4uERP2A6IA=",
    "GrBSaDMynIFAk+5XNGna1M8NWCkz45BOSwKhyTGHuSthWUA=",
    "GrhSZ+Pm2pxa05OgXI5akI4NWOkz4WAPzM5J+RlTPyvS10A=",
    "H6xSaCYRIITadE8XBGoa0dENWCk0AiDNPBQB9/TDPu1Lp8A=",
    "GrBSZ+kgnJEZA5JYGXJewpINWOkz8bNuyV7Z2UCsuSEpd4A=",
    "GrBSZ8bgXZOkc7JsEHZKh26P1ikz7GbQUz9xqduARTlwuAA=",
    "GrHSZ84ynJ4EU/JsEIohw++NWOkz6BauyQMxyRuFuy7BB0A=",
    "GrBSZ6QHnZeUA/KUPXZDeM6NWCkz9hOuSGplyW4GSSjRpwA=",
    "GqxSZ9SL4Zaxw/MSAIJC4PCNWOk0FpBNxVIqGAZBPvocF8A=",
    "GrhSZ8uCGpPWS5JvBoZawa6NWCkz/dNuSDhaGL2ntSqEpwA=",
    "GrBSZ+cDoZOkO5IQgnJdEI6NXCkz7mHuzUqqGcrfuy+EV8A=",
    "GrHSZ83JnJGgY/IMF35VAe+NVikz8lROyj19qQ+VOylwmIA=",
    "GrBSaDYhHYTsK3B8E4XvOO6NWCkz8AfOyEpR2VLBOy+BZwA=",
    "GrHSZ9gj3JPGPLIyAGpGXS4NWOkz9pBPSwil2SRAPyq4p0A=",
    "GqxSZ+Qnn42FC1O7VoIwSzCNWOk0FQLuwSIhqDwJPvRgB0A=",
    "GrBSZ+P3m5d5E7K5mnZdsm6NWCkz5vJOyEYZ2SJfuRNgt8A=",
    "GrHSZ/kz3ZdIS7LYEn5MYG4NXCkz+CWvy9Rd2d8nvzlIhoA=",
    "H7hSaA3THI7AdHBLB2otghCNWOkz8bPNwsuF2O1GPxqZd0A=",
    "GrBSZ91D3ZKEO7JyCX5fGI6NWOkz9hpuSsZp+VMAOS6wd4A=",
    "GrHSZ/1EHJOEY7KZxHZQKy6NVikz8IHvyuBl+I2BRTKYl8A=",
    "GrHSaDwiG4Ww0+8bUn3yAm+NWOkz5oJPyOBR2RWGOSEoR8A=",
    "GrBSZ6UPXZHwI7K4oHJG4U6NWOkz4wBuy7Bd2QkHuysa50A=",
    "GqxSZ+Nl4YmwM1ISAoZYEQ4NWOk0B4LQw6b1p9wVuvLwB8A=",
    "GrHSZ8amXZUkG7JUa3JNgk8NWCkz+Kcuy8uZ2UCtuSM1p0A=",
]


def _unpack(b64: str) -> list[int]:
    raw = base64.b64decode(b64)
    bits = []
    for i in range(274):
        bits.append((raw[i >> 3] >> (7 - (i & 7))) & 1)
    return bits


# Each element is a 274-length list of 0/1 ints ready for MsSpeechFrame.data.
TONE_FRAMES = [_unpack(f) for f in _PACKED_FRAMES]

