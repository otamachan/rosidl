#!/usr/bin/env python3
# Copyright 2026 Open Source Robotics Foundation, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Cache module for rosidl code generation."""

import hashlib
import json
import os
import pathlib
import shutil
from typing import Any, Dict, List, Optional, Tuple, TypedDict


class CacheConfig(TypedDict):
    """Cache configuration settings."""
    cache_dir: Optional[str]
    cache_verbose: bool
    cache_max_size: int  # Maximum cache size in bytes


_cache_config = None


def get_cache_config() -> CacheConfig:
    """
    Get cache configuration from config file and environment variables.

    Reads from ROSIDL_CACHE_CONFIG file (JSON with lowercase keys),
    then overrides with ROSIDL_* environment variables (uppercase).

    Returns:
        CacheConfig dictionary with cache settings
    """
    global _cache_config
    if _cache_config is not None:
        return _cache_config

    config: CacheConfig = {
        'cache_dir': None,
        'cache_verbose': False,
        'cache_max_size': 1073741824  # 1GB default
    }

    # Load from config file
    config_file = os.environ.get('ROSIDL_CACHE_CONFIG')
    if config_file and os.path.exists(config_file):
        with open(config_file, 'r') as f:
            file_config = json.load(f)
            for key in config.keys():
                if key in file_config:
                    config[key] = file_config[key]

    # Override with environment variables (CACHE_DIR -> cache_dir)
    for key in config.keys():
        env_key = 'ROSIDL_' + key.upper()
        env_value = os.environ.get(env_key)
        if env_value is not None:
            if isinstance(config[key], bool):
                config[key] = True  # Environment variable exists means True
            elif isinstance(config[key], int):
                config[key] = int(env_value)
            else:
                config[key] = env_value

    _cache_config = config
    return config


def is_cache_verbose():
    """Check if verbose cache logging is enabled."""
    return get_cache_config()['cache_verbose']


class CacheInfo(TypedDict):
    """Information needed for cache operations."""
    cache_key: str
    generator_name: str
    output_mapping: Dict[str, str]


def compute_cache_key(
    idl_file_path: str,
    generator_arguments_file: str,
    template_basepath: pathlib.Path,
    output_mapping: Dict[str, str],
    package_name: str,
    additional_context: Optional[Dict] = None
) -> Optional[CacheInfo]:
    """
    Compute a cache key and cache info based on input files and configuration.

    Args:
        idl_file_path: Path to the IDL file
        generator_arguments_file: Path to generator arguments file
        template_basepath: Base path for template files
        output_mapping: Dictionary mapping template files to relative output paths
        package_name: Name of the package
        additional_context: Additional context that affects generation

    Returns:
        CacheInfo containing cache_key and metadata, or None if caching is disabled
    """
    # Check if caching is enabled
    if not get_cache_config()['cache_dir']:
        return None

    # Extract generator name from arguments file path
    generator_name = pathlib.Path(generator_arguments_file).stem
    if generator_name.endswith('__arguments'):
        generator_name = generator_name[:-len('__arguments')]

    # Build cache context
    cache_context = {
        'package_name': package_name,
        'generator_name': generator_name,
        'output_mapping': output_mapping,
    }
    if additional_context:
        cache_context.update(additional_context)

    hasher = hashlib.sha256()

    # Hash IDL file content
    with open(idl_file_path, 'rb') as f:
        hasher.update(f.read())

    # Hash all template files
    for template_file in sorted(output_mapping.keys()):
        template_path = template_basepath / template_file
        if template_path.exists():
            with open(template_path, 'rb') as f:
                hasher.update(f.read())

    # Hash cache context
    context_str = json.dumps(cache_context, sort_keys=True)
    hasher.update(context_str.encode('utf-8'))

    cache_key = hasher.hexdigest()
    cache_info: CacheInfo = {
        'cache_key': cache_key,
        'generator_name': generator_name,
        'output_mapping': output_mapping,
    }

    return cache_info


def get_cache_entry_dir(
    cache_key: str,
    generator_name: str
) -> Optional[pathlib.Path]:
    """
    Get the cache entry directory for a specific cache key.

    Args:
        cache_key: The cache key (64-char hash)
        generator_name: Name of the generator (e.g., "rosidl_generator_c")

    Returns:
        Path to cache entry directory, or None if cache not configured
    """
    cache_dir_str = get_cache_config()['cache_dir']
    if not cache_dir_str:
        return None

    cache_dir = pathlib.Path(cache_dir_str)
    # Structure: $ROSIDL_CACHE_DIR/<generator_name>/<cache_key>/
    entry_dir = cache_dir / generator_name / cache_key
    return entry_dir


def cleanup_cache_if_needed(generator_name: str):
    """
    Clean up old cache entries if total size exceeds the limit.

    Args:
        generator_name: Name of the generator to clean up cache for
    """
    config = get_cache_config()
    cache_dir_str = config['cache_dir']
    max_size = config['cache_max_size']

    if not cache_dir_str:
        return

    cache_dir = pathlib.Path(cache_dir_str)
    generator_dir = cache_dir / generator_name

    if not generator_dir.exists():
        return

    # Collect all cache entries with their sizes and modification times
    entries = []
    total_size = 0

    for entry_dir in generator_dir.iterdir():
        if not entry_dir.is_dir():
            continue

        # Calculate size of this cache entry
        entry_size = 0
        try:
            for file_path in entry_dir.rglob('*'):
                if file_path.is_file():
                    entry_size += file_path.stat().st_size

            # Get oldest mtime in this entry (for sorting)
            mtime = entry_dir.stat().st_mtime

            entries.append({
                'path': entry_dir,
                'size': entry_size,
                'mtime': mtime
            })
            total_size += entry_size
        except (OSError, PermissionError):
            # Skip entries we can't access
            continue

    # Check if cleanup is needed
    if total_size <= max_size:
        return

    # Sort by modification time (oldest first)
    entries.sort(key=lambda e: e['mtime'])

    # Delete oldest entries until we're below 80% of max_size
    target_size = int(max_size * 0.8)
    current_size = total_size

    for entry in entries:
        if current_size <= target_size:
            break

        try:
            shutil.rmtree(entry['path'])
            current_size -= entry['size']
            if is_cache_verbose():
                print(f'[rosidl cache] Removed old cache entry: {entry["path"].name}')
        except (OSError, PermissionError) as e:
            if is_cache_verbose():
                print(f'[rosidl cache] Failed to remove cache entry: {e}')


def restore_from_cache_if_exists(
    idl_file_path: str,
    generator_arguments_file: str,
    template_basepath: pathlib.Path,
    output_mapping: Dict[str, str],
    package_name: str,
    additional_context: Optional[Dict],
    output_dir: str
) -> Tuple[Optional[CacheInfo], Optional[List[str]]]:
    """
    Compute cache key and restore generated files from cache if they exist.

    Args:
        idl_file_path: Path to the IDL file
        generator_arguments_file: Path to generator arguments file
        template_basepath: Base path for template files
        output_mapping: Dictionary mapping template files to relative output paths
        package_name: Name of the package
        additional_context: Additional context that affects generation
        output_dir: Output directory for generated files

    Returns:
        Tuple of (cache_info, output_files) where:
        - cache_info: CacheInfo or None if caching is disabled
        - output_files: List of absolute paths to restored files, or None if cache miss
    """
    # Compute cache key
    cache_info = compute_cache_key(
        idl_file_path=idl_file_path,
        generator_arguments_file=generator_arguments_file,
        template_basepath=template_basepath,
        output_mapping=output_mapping,
        package_name=package_name,
        additional_context=additional_context
    )

    if not cache_info:
        return (None, None)

    cache_key = cache_info['cache_key']
    generator_name = cache_info['generator_name']
    rel_generated_files = list(cache_info['output_mapping'].values())

    # Check if cache entry exists
    entry_dir = get_cache_entry_dir(cache_key, generator_name)
    if not entry_dir or not entry_dir.exists():
        return (cache_info, None)

    # Check if all expected files exist in cache
    for output_file in rel_generated_files:
        cached_file = entry_dir / output_file
        if not cached_file.exists():
            return (cache_info, None)

    # Restore files from cache
    try:
        output_path = pathlib.Path(output_dir)

        for output_file in rel_generated_files:
            cached_file = entry_dir / output_file
            dest_file = output_path / output_file

            # Create parent directory if needed
            dest_file.parent.mkdir(parents=True, exist_ok=True)

            # Copy file from cache
            shutil.copy2(cached_file, dest_file)

        if is_cache_verbose():
            print(f'[rosidl cache] Restored {len(rel_generated_files)} files from cache')
            print(f'[rosidl cache] Cache key: {cache_key[:16]}...')

        # Return cache info and absolute paths
        output_files = [os.path.join(output_dir, f) for f in rel_generated_files]
        return (cache_info, output_files)

    except Exception as e:
        if is_cache_verbose():
            print(f'[rosidl cache] Failed to restore from cache: {e}')
        return (cache_info, None)


def save_to_cache(
    cache_info: Optional[CacheInfo],
    output_dir: str
) -> bool:
    """
    Save generated files to cache.

    Args:
        cache_info: Cache information, or None if caching is disabled
        output_dir: Output directory for generated files

    Returns:
        True if successful, False otherwise
    """
    if not cache_info:
        return False

    cache_key = cache_info['cache_key']
    generator_name = cache_info['generator_name']
    rel_generated_files = list(cache_info['output_mapping'].values())

    entry_dir = get_cache_entry_dir(cache_key, generator_name)
    if not entry_dir:
        return False

    try:
        # Create cache entry directory
        entry_dir.mkdir(parents=True, exist_ok=True)

        output_path = pathlib.Path(output_dir)

        for output_file in rel_generated_files:
            src_file = output_path / output_file
            if not src_file.exists():
                continue

            cached_file = entry_dir / output_file

            # Create parent directory in cache
            cached_file.parent.mkdir(parents=True, exist_ok=True)

            # Copy file to cache
            shutil.copy2(src_file, cached_file)

        if is_cache_verbose():
            print(f'[rosidl cache] Saved {len(rel_generated_files)} files to cache')
            print(f'[rosidl cache] Cache key: {cache_key[:16]}...')

        # Clean up old cache entries if size limit exceeded
        cleanup_cache_if_needed(generator_name)

        return True

    except Exception as e:
        if is_cache_verbose():
            print(f'[rosidl cache] Failed to save to cache: {e}')
        return False
