import os
import math
import asyncio
import logging
from pathlib import Path
from typing import List, Tuple
import ffmpeg

logger = logging.getLogger(__name__)

async def get_video_duration(file_path: str) -> float:
    """Get video duration in seconds using ffprobe"""
    try:
        probe = await asyncio.create_subprocess_exec(
            'ffprobe', '-v', 'error', '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1', file_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await probe.communicate()
        return float(stdout.decode().strip())
    except Exception as e:
        logger.error(f"Error getting video duration: {e}")
        return 0

async def split_video(input_path: str, target_size: int = 1.95*1024*1024*1024) -> List[str]:
    """Split video into parts smaller than target_size (default 1.95GB)"""
    duration = await get_video_duration(input_path)
    if duration == 0:
        raise ValueError("Could not determine video duration")

    # Get video bitrate
    probe = await asyncio.create_subprocess_exec(
        'ffprobe', '-v', 'error', '-select_streams', 'v:0',
        '-show_entries', 'stream=bit_rate', '-of', 'default=noprint_wrappers=1:nokey=1',
        input_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await probe.communicate()
    bit_rate = int(stdout.decode().strip())

    # Calculate number of parts needed
    file_size = os.path.getsize(input_path)
    num_parts = math.ceil(file_size / target_size)
    segment_duration = duration / num_parts
    
    output_files = []
    input_filename = Path(input_path).stem

    for i in range(num_parts):
        start_time = i * segment_duration
        output_path = f"{input_path}_{i+1}.mp4"
        
        cmd = [
            'ffmpeg', '-i', input_path,
            '-ss', str(start_time),
            '-t', str(segment_duration),
            '-c', 'copy',
            '-y',
            output_path
        ]
        
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        await process.communicate()
        if os.path.exists(output_path):
            output_files.append(output_path)
            
    return output_files

async def cleanup_split_files(file_paths: List[str]):
    """Clean up split video files"""
    for file_path in file_paths:
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
        except Exception as e:
            logger.error(f"Error cleaning up file {file_path}: {e}")
