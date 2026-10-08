#!/usr/bin/env python3
"""Decode theme_icons.json into individual .ico files.
Run this in the assets/ folder to generate all theme icon variants.
"""
import json, base64, os

def decode_icons(manifest_path: str = "theme_icons.json", output_dir: str = "icons"):
    """Decode all base64-encoded .ico files from the manifest."""
    with open(manifest_path, 'r') as f:
        manifest = json.load(f)
    
    os.makedirs(output_dir, exist_ok=True)
    
    for key, icon_data in manifest["icons"].items():
        filename = icon_data["filename"]
        ico_bytes = base64.b64decode(icon_data["data_base64"])
        
        output_path = os.path.join(output_dir, filename)
        with open(output_path, 'wb') as f:
            f.write(ico_bytes)
        
        print(f"  ✓ {filename} ({len(ico_bytes):,} bytes) — {icon_data['theme']} [{icon_data['accent']}]")
    
    print(f"\nDone! {len(manifest['icons'])} icons saved to {output_dir}/")

if __name__ == "__main__":
    decode_icons()
