#!/usr/bin/env python3
import os
import sys
import json
import yaml
import re
import subprocess
import requests
import argparse
from pathlib import Path


def get_latest_build():
    """Get the latest build number"""
    url = "https://developers.eveonline.com/static-data/tranquility/latest.jsonl"
    response = requests.get(url, timeout=10)
    for line in response.text.strip().splitlines():
        data = json.loads(line)
        if data.get("_key") == "sde":
            return data.get("buildNumber", None)
    return None


def get_build_number_from_resindex():
    """Get build number from resindex"""
    url = "https://binaries.eveonline.com/eveonline_latest.txt"
    response = requests.get(url, timeout=10)
    for line in response.text.strip().splitlines():
        if line.startswith("resfileindex.txt"):
            parts = line.lower().split(",")
            if len(parts) > 1:
                return parts[1].split("_")[0] if "_" in parts[1] else None
    return None


def download_resfileindex(build):
    """Download resfileindex.txt"""
    url = f"https://binaries.eveonline.com/eveonline_{build}.txt"
    response = requests.get(url, timeout=30)
    resfileindex_hash = None
    for line in response.text.strip().splitlines():
        if line.lower().startswith("app:/resfileindex.txt"):
            parts = line.lower().split(",")
            if len(parts) > 1:
                resfileindex_hash = parts[1]
                break
    
    if not resfileindex_hash:
        return None
    
    download_url = f"https://binaries.eveonline.com/{resfileindex_hash}"
    resfileindex_response = requests.get(download_url, timeout=30)
    
    resfiles = {}
    for line in resfileindex_response.text.strip().splitlines():
        if not line:
            continue
        parts = line.lower().split(",")
        if len(parts) >= 2:
            res_path = parts[0].split(":/")[1] if ":/" in parts[0] else parts[0]
            resfile_hash = parts[1]
            resfiles[res_path] = resfile_hash
    
    return resfiles


def download_resfiledependencies(build):
    """Download resfiledependencies.yaml"""
    url = f"https://binaries.eveonline.com/eveonline_{build}.txt"
    response = requests.get(url, timeout=30)
    dependencies_hash = None
    for line in response.text.strip().splitlines():
        if line.lower().startswith("app:/resfiledependencies.yaml"):
            parts = line.lower().split(",")
            if len(parts) > 1:
                dependencies_hash = parts[1]
                break
    
    if not dependencies_hash:
        return {}
    
    download_url = f"https://binaries.eveonline.com/{dependencies_hash}"
    dependencies_response = requests.get(download_url, timeout=30)
    return yaml.safe_load(dependencies_response.text) or {}


def get_ship_info_from_sde(type_id):
    """Get ship information from SDE"""
    latest_url = "https://developers.eveonline.com/static-data/tranquility/latest.jsonl"
    response = requests.get(latest_url, timeout=10)
    build = None
    for line in response.text.strip().splitlines():
        data = json.loads(line)
        if data.get("_key") == "sde":
            build = data.get("buildNumber")
            break
    
    if not build:
        return None
    
    # Use fixed sde_cache directory
    sde_cache_dir = "sde_cache"
    os.makedirs(sde_cache_dir, exist_ok=True)
    
    # Check if graphics.jsonl already exists (files are extracted directly to sde_cache_dir)
    graphics_path = os.path.join(sde_cache_dir, "graphics.jsonl")
    types_path = os.path.join(sde_cache_dir, "types.jsonl")
    
    if not os.path.exists(graphics_path) or not os.path.exists(types_path):
        sde_base = f"https://developers.eveonline.com/static-data/tranquility/eve-online-static-data-{build}-jsonl.zip"
        zip_path = os.path.join(sde_cache_dir, f"sde-{build}.zip")
        
        print(f"Downloading SDE zip to {zip_path}...")
        zip_response = requests.get(sde_base, timeout=60, stream=True)
        with open(zip_path, "wb") as f:
            for chunk in zip_response.iter_content(chunk_size=8192):
                f.write(chunk)
        
        print(f"Extracting SDE zip...")
        import zipfile
        with zipfile.ZipFile(zip_path, "r") as zip_ref:
            zip_ref.extractall(sde_cache_dir)
    
    # Read graphics.jsonl
    graphics_info = {}
    print(f"Reading graphics.jsonl from: {graphics_path}")
    if not os.path.exists(graphics_path):
        print(f"Error: graphics.jsonl not found at {graphics_path}")
        return None
    
    with open(graphics_path, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            data = json.loads(line)
            graphic_id = data.get("_key")
            if graphic_id:
                graphics_info[int(graphic_id)] = {
                    "iconFolder": data.get("iconFolder", ""),
                    "sofHullName": data.get("sofHullName", ""),
                    "sofRaceName": data.get("sofRaceName", ""),
                }
    
    # Read types.jsonl
    print(f"Reading types.jsonl from: {types_path}")
    if not os.path.exists(types_path):
        print(f"Error: types.jsonl not found at {types_path}")
        return None
    
    with open(types_path, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            data = json.loads(line)
            if str(data.get("_key")) == str(type_id):
                graphic_id = data.get("graphicID")
                if graphic_id and int(graphic_id) in graphics_info:
                    return graphics_info[int(graphic_id)]
    
    return None


def is_filtered_file(dep_path, icon_folder=None, sof_race_name=None):
    """Check if file should be filtered"""
    dep_lower = dep_path.lower()
    
    # Keep all .gr2 files
    if dep_lower.endswith(".gr2"):
        return True
    
    # Filter texture files
    if dep_lower.endswith(".dds") or dep_lower.endswith(".png") or dep_lower.endswith(".jpg"):
        # Filter out low quality textures
        file_name = os.path.basename(dep_path)
        if "_lowdetail" in file_name or "_mediumdetail" in file_name:
            return False
        return True
    
    return False


def get_ship_dependencies(sof_hull_name, resfile_dependencies):
    """Get all dependencies for a ship"""
    if not sof_hull_name or not resfile_dependencies:
        return []
    
    key = f"res:/dx9/model/spaceobjectfactory/hulls/{sof_hull_name}.red"
    dependencies = resfile_dependencies.get(key, [])
    return dependencies if isinstance(dependencies, list) else []


def download_file(res_path, resfiles, output_path):
    """Download file from online server"""
    res_path_normalized = res_path.lower()
    if res_path_normalized.startswith("res:/"):
        res_path_normalized = res_path_normalized[5:]
    
    resfile_hash = resfiles.get(res_path_normalized)
    if not resfile_hash:
        return False
    
    url = f"https://resources.eveonline.com/{resfile_hash}"
    response = requests.get(url, timeout=30)
    if response.status_code == 200:
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "wb") as f:
            f.write(response.content)
        return True
    return False


def convert_gr2_to_obj(gr2_path):
    """Convert GR2 to OBJ"""
    gr2_json_path = f"{gr2_path}.gr2_json"
    
    gr2tojson_exe = os.path.join("tools", "gr2tojson", "gr2tojson.exe")
    subprocess.run([gr2tojson_exe, gr2_path], check=False, capture_output=True)
    
    if not os.path.exists(gr2_json_path):
        return False
    
    with open(gr2_json_path, "r", encoding="utf-8") as f:
        content = re.sub(r"\-nan\(ind\)|\-inf|inf", "0", f.read())
        gr2_json = json.loads(content)
    
    obj_lines = []
    model_offset = 1
    
    for mesh in gr2_json.get("meshes", []):
        obj_lines.append(f"o {mesh['name']}")
        
        vertex = mesh.get("vertex", {})
        positions = vertex.get("position", [])
        texcoords = vertex.get("texcoord0", [])
        normals = vertex.get("normal", [])
        
        for i in range(0, len(positions), 3):
            obj_lines.append(f"v {positions[i]} {positions[i+1]} {positions[i+2]}")
        
        for i in range(0, len(texcoords), 2):
            obj_lines.append(f"vt {texcoords[i]} {texcoords[i+1]}")
        
        if normals:
            for i in range(0, len(normals), 3):
                obj_lines.append(f"vn {normals[i]} {normals[i+1]} {normals[i+2]}")
        
        obj_lines.append("s 1")
        
        for indice in mesh.get("indices", []):
            obj_lines.append(f"usemtl {indice['name']}")
            faces = indice.get("faces", [])
            for i in range(0, len(faces), 3):
                v1 = faces[i] + model_offset
                v2 = faces[i + 1] + model_offset
                v3 = faces[i + 2] + model_offset
                obj_lines.append(f"f {v1}/{v1}/{v1} {v2}/{v2}/{v2} {v3}/{v3}/{v3}")
        
        model_offset += len(positions) // 3
    
    obj_path = gr2_path.replace(".gr2", ".obj")
    with open(obj_path, "w", encoding="utf-8") as f:
        f.write("\n".join(obj_lines))
    
    os.remove(gr2_json_path)
    return True


def convert_dds_to_png(dds_path):
    """Convert DDS to PNG"""
    nvtt_exe = os.path.join("tools", "dds2png", "nvtt_export.exe")
    dir_path = os.path.dirname(dds_path)
    filename = os.path.splitext(os.path.basename(dds_path))[0]
    png_path = os.path.join(dir_path, f"{filename}.png")
    
    subprocess.run([nvtt_exe, dds_path, "-o", png_path], check=False, capture_output=True)
    
    if os.path.exists(png_path):
        os.remove(dds_path)
        return True
    return False


def main():
    parser = argparse.ArgumentParser(description="Export ship resources")
    parser.add_argument("type_id", type=int, help="Ship TypeID")
    args = parser.parse_args()
    
    print(f"Fetching ship information (TypeID: {args.type_id})...")
    ship_info = get_ship_info_from_sde(args.type_id)
    if not ship_info:
        print(f"Error: Cannot find ship information for TypeID {args.type_id}")
        sys.exit(1)
    
    sof_hull_name = ship_info.get("sofHullName", "")
    if not sof_hull_name:
        print(f"Error: Ship does not have sofHullName")
        sys.exit(1)
    
    print(f"Ship info: sofHullName={sof_hull_name}")
    
    # Get build number
    print("Fetching build number...")
    build = get_build_number_from_resindex()
    if not build:
        build = get_latest_build()
    if not build:
        print("Error: Cannot get build number")
        sys.exit(1)
    
    print(f"Build number: {build}")
    
    # Download resfileindex and resfiledependencies
    print("Downloading resfileindex.txt...")
    resfiles = download_resfileindex(build)
    if not resfiles:
        print("Error: Cannot download resfileindex.txt")
        sys.exit(1)
    
    print("Downloading resfiledependencies.yaml...")
    resfile_dependencies = download_resfiledependencies(build)
    if not resfile_dependencies:
        print("Error: Cannot download resfiledependencies.yaml")
        sys.exit(1)
    
    # Get all dependencies
    print("Fetching dependency list...")
    all_dependencies = get_ship_dependencies(sof_hull_name, resfile_dependencies)
    if not all_dependencies:
        print(f"Error: Cannot find dependencies for ship {sof_hull_name}")
        sys.exit(1)
    
    print(f"Found {len(all_dependencies)} dependency files")
    
    # Filter dependencies
    icon_folder = ship_info.get("iconFolder", "")
    sof_race_name = ship_info.get("sofRaceName", "")
    filtered_dependencies = []
    
    for dep_path in all_dependencies:
        full_dep_path = dep_path if dep_path.startswith("res:/") else f"res:/{dep_path}"
        if is_filtered_file(full_dep_path, icon_folder, sof_race_name):
            filtered_dependencies.append(dep_path)
    
    print(f"After filtering: {len(filtered_dependencies)} files remaining")
    
    # Create output directory
    output_dir = "output"
    os.makedirs(output_dir, exist_ok=True)
    
    # Download and convert files
    print("Downloading and converting files...")
    for i, dep_path in enumerate(filtered_dependencies, 1):
        print(f"[{i}/{len(filtered_dependencies)}] Processing: {dep_path}")
        
        filename = os.path.basename(dep_path)
        output_path = os.path.join(output_dir, filename)
        
        if download_file(dep_path, resfiles, output_path):
            if output_path.lower().endswith(".gr2"):
                print(f"  Converting GR2 -> OBJ...")
                if convert_gr2_to_obj(output_path):
                    print(f"  [+] Converted to OBJ")
            elif output_path.lower().endswith(".dds"):
                print(f"  Converting DDS -> PNG...")
                if convert_dds_to_png(output_path):
                    print(f"  [+] Converted to PNG")
        else:
            print(f"  [!] Download failed")
    
    print(f"\nComplete! Files saved to {output_dir} directory")


if __name__ == "__main__":
    main()
