#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Multi-Map Exporter a python script for Kachaka Fleet Manager configuration on edge PC.

Author: Jimmy Majumder
Version: 1.0.0 (February 2025 edition)
Date: 2025-02-18 
Version: 1.0.2
Date: 2025-06-12 | July 2025 edition
Copyright: QibiTech Inc. 

Description:
This enhanced script exports maps from multiple Kachaka robots to multiple edge PCs based on a
configurable mapping. It facilitates advanced fleet management by automating map data extraction
and ensuring structured storage across distributed edge computing infrastructure.

Functionality:
- Establishes gRPC connections with multiple Kachaka robots based on YAML configuration
- Extracts and saves map data (PNG, JPG, YAML metadata, binary cursor files, waypoints)
- Transfers map files securely to configured edge PCs using SCP with password authentication
- Supports primary and backup storage locations on each edge PC
- Validates map data integrity before and after transfer
- Provides comprehensive logging and detailed success/failure reporting
- Handles connection failures gracefully with retry mechanisms
- Generates a detailed summary report of all operations

System Requirements:
- sshpass: Required for non-interactive SCP file transfers. Consider SSH key-based
  authentication for enhanced security in production environments.
- gRPC: Ensure the gRPC service is running and accessible on each Kachaka robot.
- SSH access: All edge PCs must permit SSH connections from the executing system.
- Python 3.6+: With required packages (grpc, pyyaml, pillow)

Security Considerations:
- The script prompts for SSH passwords at runtime; they are not stored in plaintext.
- It is recommended to use SSH key-based authentication for production deployments.
- Ensure that all edge PCs are secure and have proper access controls in place.
- The script handles passwords securely in memory and does not write them to disk.

Configuration:
The script uses a YAML configuration file (default: multi_map_exporter_config.yml) with:
- Robot definitions (IP, name, target edge PCs)
- Edge PC definitions (IP, username, description, map directories)
- Connection parameters (timeouts, retry settings)

Usage:
Execute the script: python3 multi_map_exporter.py

***Note:
Before running the script, please ensure you have read the following documentation:
README - Setup & Procedure: 
https://bitbucket.org/qibitech/kachaka-api/src/b2bfce371dcf0a36132c082d348521c0085e94ba/python/demos/README_SMART_SPEAKER.md?at=feature%2Fmulti_map_setup_fleet_manager

Prerequisites:
1. git clone https://github.com/pf-robotics/kachaka-api.git # clone the repository
2. python3 -m venv venv # do this if you want to create a virtual environment
3. source venv/bin/activate # do this if you created a virtual environment
4. cd kachaka-api/python/demos # go to the directory where the script is located
5. pip install -r requirements.txt
6. cd grpc_samples
7. find ../../../../ -name "kachaka-api.proto" # find the proto file location
8. python3 -m grpc_tools.protoc -I../../../protos --python_out=. --pyi_out=. --grpc_python_out=. ../../../protos/kachaka-api.proto # make sure where the proto file is located
9. Create the configuration file (multi_map_exporter_config.yml) in the same directory
10. python3 multi_map_exporter.py # run the script
"""

import sys
import grpc
import kachaka_api_pb2
from kachaka_api_pb2_grpc import KachakaApiStub
import yaml
import struct
import threading
import re
import subprocess
import os
import getpass
import logging
import time
import argparse
from datetime import datetime
from PIL import Image
import io
from typing import List, Dict, Tuple, Optional, Any

# Default configuration file path
CONFIG_FILE = "multi_map_exporter_config.yml"

def parse_arguments():
    """
    Parse command line arguments.
    
    Returns:
        argparse.Namespace: Parsed command line arguments containing the configuration file path.
    """
    parser = argparse.ArgumentParser(description='Kachaka Map Exporter')
    parser.add_argument('--config', type=str, default=CONFIG_FILE,
                        help=f'Path to configuration file (default: {CONFIG_FILE})')
    return parser.parse_args()

def load_config(config_file):
    """
    Load configuration from YAML file.
    
    Args:
        config_file (str): Path to the configuration file.
    
    Returns:
        dict: Loaded configuration as a dictionary.
    """
    try:
        with open(config_file, 'r') as f:
            config = yaml.safe_load(f)
        
        # Set default values for missing configuration items
        if 'connection' not in config:
            config['connection'] = {}
        
        # Set default connection settings if not specified
        connection_defaults = {
            'max_retries': 2,
            'retry_delay': 3,
            'grpc_port': 26400,
            'transfer_timeout': 30
        }
        
        for key, default_value in connection_defaults.items():
            if key not in config['connection']:
                config['connection'][key] = default_value
        
        # Add password field to edge PCs
        for edge_pc in config['edge_pcs']:
            edge_pc['password'] = None
            
        return config
    except FileNotFoundError:
        print(f"ERROR: Configuration file '{config_file}' not found.")
        print(f"Please create the configuration file or specify a different file with --config.")
        sys.exit(1)
    except yaml.YAMLError as e:
        print(f"ERROR: Invalid YAML in configuration file: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: Failed to load configuration: {e}")
        sys.exit(1)

# Logging Setup
LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, f"kachaka_map_transfer_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# Status tracking
transfer_status = {
    "robots": {},
    "edge_pcs": {},
    "overall_success": False,
    "robot_to_edge_mapping": {}  # Store the mapping for summary report
}

def _log_message(level, message):
    """Logs messages to both console and log file."""
    print(message)  # Always print to console
    logging.log(level, message)

def log_info(message):
    """Logs an informational message."""
    _log_message(logging.INFO, message)

def log_error(message):
    """Logs an error message."""
    _log_message(logging.ERROR, f"ERROR: {message}")

def log_warning(message):
    """Logs a warning message."""
    _log_message(logging.WARNING, f"WARNING: {message}")

def log_success(message):
    """Logs a success message."""
    _log_message(logging.INFO, f"SUCCESS: {message}")

def check_ssh_connection_with_retry(edge_pc, max_attempts=3):
    """
    Verifies SSH connectivity to an edge PC with interactive retry.
    
    Args:
        edge_pc (dict): Edge PC configuration including username, IP, description, and password.
        max_attempts (int): Maximum number of connection attempts.
        
    Returns:
        bool: True if connection is successful, False otherwise.
    """
    username = edge_pc["username"]
    ip = edge_pc["ip"]
    description = edge_pc["description"]
    
    for attempt in range(1, max_attempts + 1):
        try:
            log_info(f"Testing SSH connection to {description} ({username}@{ip})... Attempt {attempt}/{max_attempts}")
            ssh_command = [
                "sshpass", "-p", edge_pc["password"], "ssh",
                "-o", "ConnectTimeout=5",  # Add timeout for faster failure
                f"{username}@{ip}",
                "echo 'SSH connection successful'"
            ]
            result = subprocess.run(ssh_command, check=True, capture_output=True, text=True, timeout=10)
            
            if "SSH connection successful" in result.stdout:
                log_success(f"SSH connection to {description} ({username}@{ip}) is successful")
                return True
            else:
                log_error(f"SSH connection to {description} ({username}@{ip}) failed: Unexpected response")
        except subprocess.CalledProcessError as e:
            if "Permission denied" in (e.stderr or ""):
                log_error(f"SSH connection to {description} ({username}@{ip}) failed: Incorrect password")
            elif "Could not resolve hostname" in (e.stderr or ""):
                log_error(f"SSH connection to {description} ({username}@{ip}) failed: Hostname could not be resolved")
            else:
                log_error(f"SSH connection to {description} ({username}@{ip}) failed: {e.stderr}")
        except subprocess.TimeoutExpired:
            log_error(f"SSH connection to {description} ({username}@{ip}) timed out: Server not responding")
        except Exception as e:
            log_error(f"Unexpected error connecting to {description} ({username}@{ip}): {str(e)}")
        
        # If we get here, the connection failed
        if attempt < max_attempts:
            # Ask if user wants to retry
            retry = input(f"Connection to {description} failed. Do you want to try again? (Y/N): ")
            if retry.lower() != 'y':
                log_info(f"Skipping further connection attempts to {description}")
                break
            
            # Get a new password
            edge_pc["password"] = getpass.getpass(f"Enter SSH password for {description} ({username}@{ip}): ")
    
    # If we get here, all attempts failed or user chose not to retry
    log_error(f"Failed to connect to {description} ({username}@{ip}) after {attempt} attempt(s)")
    return False

def sanitize_filename(filename):
    """
    Sanitizes a filename by replacing invalid characters with underscores.
    
    Args:
        filename (str): The filename to sanitize.
        
    Returns:
        str: The sanitized filename.
    """
    return re.sub(r'[^\w\-_\.]', '_', filename)

def ensure_home_directory_exists(edge_pc, directory):
    """
    Ensures the specified directory exists with proper permissions in the user's home directory.
    
    Args:
        edge_pc (dict): Edge PC configuration including username, IP, description, and password.
        directory (str): Directory path to ensure exists.
        
    Returns:
        bool: True if directory exists or was created, False otherwise.
    """
    username = edge_pc["username"]
    ip = edge_pc["ip"]
    password = edge_pc["password"]
    description = edge_pc["description"]
    
    try:
        # First check if the home directory exists
        check_command = [
            "sshpass", "-p", password, "ssh",
            f"{username}@{ip}",
            f"[ -d /home/{username} ] && echo 'exists' || echo 'not exists'"
        ]
        
        result = subprocess.run(check_command, check=True, capture_output=True, text=True, timeout=10)
        
        if "not exists" in result.stdout:
            log_error(f"Home directory /home/{username} does not exist on {description} ({ip})!")
            return False
        
        # Create the map directory with proper permissions
        mkdir_command = [
            "sshpass", "-p", password, "ssh",
            f"{username}@{ip}",
            f"mkdir -p {directory} && chmod 755 {directory}"
        ]
        
        subprocess.run(mkdir_command, check=True, capture_output=True, text=True, timeout=10)
        log_info(f"Successfully created and set permissions for {directory} on {description}")
        return True
        
    except subprocess.CalledProcessError as e:
        log_error(f"Error setting up directory on {description}: {e.stderr if hasattr(e, 'stderr') else str(e)}")
        return False
    except subprocess.TimeoutExpired:
        log_error(f"Command timed out when setting up directory on {description}")
        return False
    except Exception as e:
        log_error(f"Unexpected error setting up directory on {description}: {str(e)}")
        return False

def ensure_system_directory_exists(edge_pc, directory):
    """
    Ensures a system directory exists with proper permissions, using sudo if necessary.
    
    Args:
        edge_pc (dict): Edge PC configuration including username, IP, description, and password.
        directory (str): System directory path to ensure exists.
        
    Returns:
        bool: True if directory exists or was created, False otherwise.
    """
    username = edge_pc["username"]
    ip = edge_pc["ip"]
    password = edge_pc["password"]
    description = edge_pc["description"]
    
    try:
        # Check if directory exists first
        check_command = [
            "sshpass", "-p", password, "ssh",
            f"{username}@{ip}",
            f"[ -d {directory} ] && echo 'exists' || echo 'not exists'"
        ]
        
        result = subprocess.run(check_command, check=True, capture_output=True, text=True, timeout=10)
        
        if "not exists" in result.stdout:
            # Try to create the directory with sudo
            log_info(f"System directory {directory} does not exist on {description}, attempting to create with sudo...")
            
            # Create command that uses echo to pipe the password to sudo
            mkdir_command = [
                "sshpass", "-p", password, "ssh",
                f"{username}@{ip}",
                f"echo '{password}' | sudo -S mkdir -p {directory} && echo '{password}' | sudo -S chmod 777 {directory}"
            ]
            
            subprocess.run(mkdir_command, check=True, capture_output=True, text=True, timeout=15)
            log_info(f"Successfully created system directory {directory} on {description}")
        else:
            # Directory exists, ensure we have write permissions
            chmod_command = [
                "sshpass", "-p", password, "ssh",
                f"{username}@{ip}",
                f"echo '{password}' | sudo -S chmod 777 {directory}"
            ]
            
            subprocess.run(chmod_command, check=True, capture_output=True, text=True, timeout=10)
            log_info(f"Ensured write permissions for system directory {directory} on {description}")
        
        return True
        
    except subprocess.CalledProcessError as e:
        log_error(f"Error setting up system directory on {description}: {e.stderr if hasattr(e, 'stderr') else str(e)}")
        return False
    except subprocess.TimeoutExpired:
        log_error(f"Command timed out when setting up system directory on {description}")
        return False
    except Exception as e:
        log_error(f"Unexpected error setting up system directory on {description}: {str(e)}")
        return False

def create_remote_directory(robot_id, edge_pc, base_dir):
    """
    Creates a directory on the edge PC for storing map data.
    
    Args:
        robot_id (str): Robot directory name.
        edge_pc (dict): Edge PC configuration including username, IP, description, and password.
        base_dir (str): Base directory path.
        
    Returns:
        bool: True if directory was created, False otherwise.
    """
    username = edge_pc["username"]
    ip = edge_pc["ip"]
    password = edge_pc["password"]
    description = edge_pc["description"]
    
    try:
        # Check if this is a system directory that might need sudo
        if base_dir.startswith("/usr/") or base_dir.startswith("/opt/"):
            ssh_command = [
                "sshpass", "-p", password, "ssh",
                f"{username}@{ip}",
                f"echo '{password}' | sudo -S mkdir -p {base_dir}{robot_id} && echo '{password}' | sudo -S chmod 777 {base_dir}{robot_id}"
            ]
        else:
            ssh_command = [
                "sshpass", "-p", password, "ssh",
                                f"{username}@{ip}",
                f"mkdir -p {base_dir}{robot_id}"
            ]
        
        result = subprocess.run(ssh_command, check=True, capture_output=True, text=True, timeout=15)
        log_info(f"Successfully created directory {base_dir}{robot_id} on {description}")
        return True
    except subprocess.CalledProcessError as e:
        log_error(f"Error creating directory {base_dir}{robot_id} on {description}: {e.stderr if hasattr(e, 'stderr') else str(e)}")
        return False
    except subprocess.TimeoutExpired:
        log_error(f"Command timed out when creating directory {base_dir}{robot_id} on {description}")
        return False
    except Exception as e:
        log_error(f"Unexpected error creating directory {base_dir}{robot_id} on {description}: {str(e)}")
        return False

def transfer_files_scp(source_file, robot_id, edge_pc, destination, retry_count=0):
    """
    Transfers files to the edge PC using SCP with retry capability.
    
    Args:
        source_file (str): Path to the file to transfer.
        robot_id (str): Robot directory name.
        edge_pc (dict): Edge PC configuration including username, IP, description, and password.
        destination (str): Destination directory path.
        retry_count (int): Current retry attempt.
        
    Returns:
        bool: True if file was transferred, False otherwise.
    """
    username = edge_pc["username"]
    ip = edge_pc["ip"]
    password = edge_pc["password"]
    description = edge_pc["description"]
    max_retries = MAX_RETRIES
    
    try:
        robot_dir = f"{destination}{robot_id}/"
        scp_command = [
            "sshpass", "-p", password, "scp",
            "-o", "ConnectTimeout=10",  # Add timeout for faster failure
            source_file,
            f"{username}@{ip}:{robot_dir}"
        ]
        
        subprocess.run(scp_command, check=True, capture_output=True, text=True, timeout=TRANSFER_TIMEOUT)
        log_info(f"Successfully transferred {os.path.basename(source_file)} to {robot_dir} on {description}")
        return True
    except subprocess.CalledProcessError as e:
        log_error(f"Error transferring {os.path.basename(source_file)} to {description}: {e.stderr if hasattr(e, 'stderr') else str(e)}")
    except subprocess.TimeoutExpired:
        log_error(f"Transfer of {os.path.basename(source_file)} to {description} timed out")
    except Exception as e:
        log_error(f"Unexpected error transferring {os.path.basename(source_file)} to {description}: {str(e)}")
    
    # If we get here, the transfer failed
    if retry_count < max_retries:
        log_warning(f"Retrying transfer of {os.path.basename(source_file)} to {description} (Attempt {retry_count + 1}/{max_retries})")
        time.sleep(RETRY_DELAY)  # Wait before retrying
        return transfer_files_scp(source_file, robot_id, edge_pc, destination, retry_count + 1)
    else:
        log_error(f"Failed to transfer {os.path.basename(source_file)} to {description} after {max_retries} attempts")
        return False

def validate_image_file(file_path):
    """
    Validates that an image file is readable and not corrupted.
    
    Args:
        file_path (str): Path to the image file.
        
    Returns:
        bool: True if file is valid, False otherwise.
    """
    try:
        with Image.open(file_path) as img:
            # Try to load the image data
            img.verify()
            # Get image dimensions to ensure it's valid
            width, height = img.size
            if width <= 0 or height <= 0:
                log_error(f"Invalid image dimensions in {file_path}: {width}x{height}")
                return False
            
            log_info(f"Validated image file {file_path}: {width}x{height} pixels")
            return True
    except Exception as e:
        log_error(f"Failed to validate image file {file_path}: {str(e)}")
        return False

def validate_yaml_file(file_path):
    """
    Validates that a YAML file is readable and contains expected fields.
    
    Args:
        file_path (str): Path to the YAML file.
        
    Returns:
        bool: True if file is valid, False otherwise.
    """
    try:
        with open(file_path, 'r') as f:
            data = yaml.safe_load(f)
        
        # Check for required fields
        required_fields = ['name', 'resolution', 'width', 'height', 'origin']
        for field in required_fields:
            if field not in data:
                log_error(f"Missing required field '{field}' in YAML file {file_path}")
                return False
        
        # Check origin structure
        if not isinstance(data['origin'], dict) or not all(k in data['origin'] for k in ['x', 'y', 'theta']):
            log_error(f"Invalid 'origin' structure in YAML file {file_path}")
            return False
        
        log_info(f"Validated YAML file {file_path}")
        return True
    except yaml.YAMLError as e:
        log_error(f"Invalid YAML format in {file_path}: {str(e)}")
        return False
    except Exception as e:
        log_error(f"Failed to validate YAML file {file_path}: {str(e)}")
        return False

def validate_binary_file(file_path):
    """
    Validates that a binary file is readable and contains expected data.
    
    Args:
        file_path (str): Path to the binary file.
        
    Returns:
        bool: True if file is valid, False otherwise.
    """
    try:
        with open(file_path, 'rb') as f:
            binary_data = f.read(8)
            if len(binary_data) != 8:
                log_error(f"Binary file {file_path} does not contain expected 8 bytes of data")
                return False
            
            # Try to unpack the cursor value
            cursor_value = struct.unpack("<q", binary_data)[0]
            log_info(f"Validated binary file {file_path}: Cursor value = {cursor_value}")
            return True
    except struct.error as e:
        log_error(f"Failed to unpack binary data in {file_path}: {str(e)}")
        return False
    except Exception as e:
        log_error(f"Failed to validate binary file {file_path}: {str(e)}")
        return False

def get_map_data(robot_config, edge_pc_passwords):
    """
    Retrieves map data from a Kachaka robot and transfers to specified edge PCs.
    
    Args:
        robot_config (dict): Robot configuration including IP, name, and target edge PCs.
        edge_pc_passwords (dict): Dictionary of edge PC passwords keyed by edge PC ID.
    """
    robot_ip = robot_config["ip"]
    robot_name = robot_config["name"]
    target_edge_pcs = robot_config["target_edge_pcs"]
    
    # Initialize status tracking for this robot
    transfer_status["robots"][robot_name] = {
        "connected": False,
        "map_retrieved": False,
        "transfers": {}
    }
    
    # Add to mapping for summary report
    for edge_pc_id in target_edge_pcs:
        if edge_pc_id not in transfer_status["robot_to_edge_mapping"]:
            transfer_status["robot_to_edge_mapping"][edge_pc_id] = []
        transfer_status["robot_to_edge_mapping"][edge_pc_id].append(robot_name)
    
    grpc_address = f"{robot_ip}:{GRPC_PORT}"
    log_info(f"Connecting to robot {robot_name} at {grpc_address}...")
    
    try:
        # Create gRPC channel with timeout
        channel = grpc.insecure_channel(grpc_address)
        # Create a deadline 10 seconds from now
        deadline = time.time() + 10
        # Wait for the channel to be ready
        grpc.channel_ready_future(channel).result(timeout=10)
        
        stub = KachakaApiStub(channel)
        
        # Get robot serial number
        serial_number_response = stub.GetRobotSerialNumber(kachaka_api_pb2.GetRequest())
        robot_serial_number = serial_number_response.serial_number
        log_info(f"Connected to robot {robot_name} (Serial: {robot_serial_number})")
        
        # Update status
        transfer_status["robots"][robot_name]["connected"] = True
        transfer_status["robots"][robot_name]["serial"] = robot_serial_number
        
        # Get current map ID
        current_map_id_response = stub.GetCurrentMapId(kachaka_api_pb2.GetRequest())
        map_id = current_map_id_response.id
        log_info(f"Current map ID for {robot_name}: {map_id}")
        
        # Get robot version
        version_response = stub.GetRobotVersion(kachaka_api_pb2.GetRequest())
        log_info(f"Robot version for {robot_name}: {version_response}")
        
        # Get map data
        map_response = stub.GetPngMap(kachaka_api_pb2.GetRequest())
        log_info(f"Retrieved map data for {robot_name}")
        
        # Update status
        transfer_status["robots"][robot_name]["map_retrieved"] = True
        
        # Process map data
        map_name = sanitize_filename(map_response.map.name)
        filename_prefix = f"{map_name}"
        png_filename = f"{filename_prefix}.png"
        yaml_filename = f"{filename_prefix}_metadata.yaml"
        bin_filename = f"{filename_prefix}_metadata.bin"
        jpg_filename = f"{filename_prefix}.jpg"
        waypoints_filename = f"{filename_prefix}_waypoints.loc"
        
        # Create a directory for this robot locally
        robot_dir = f"Kachaka_{robot_serial_number}"
        os.makedirs(robot_dir, exist_ok=True)
        log_info(f"Created local directory: {robot_dir}")
        
        # Save PNG file
        png_path = os.path.join(robot_dir, png_filename)
        with open(png_path, "wb") as binary_file:
            binary_file.write(map_response.map.data)
        log_info(f"Saved {png_filename} locally")
        
        # Validate PNG file
        if not validate_image_file(png_path):
            log_error(f"PNG file validation failed for {robot_name}")
        
        # Convert PNG to JPG
        jpg_path = os.path.join(robot_dir, jpg_filename)
        try:
            png_data = io.BytesIO(map_response.map.data)
            img = Image.open(png_data)
            img.convert('RGB').save(jpg_path, 'JPEG', quality=95)
            log_info(f"Converted and saved {jpg_filename} locally")
            
            # Validate JPG file
            if not validate_image_file(jpg_path):
                log_error(f"JPG file validation failed for {robot_name}")
        except Exception as e:
            log_error(f"Error creating JPG version of the map for {robot_name}: {e}")
        
        # Create and save YAML metadata
        map_metadata = {
            "name": map_response.map.name,
            "resolution": map_response.map.resolution,
            "width": map_response.map.width,
            "height": map_response.map.height,
            "origin": {
                "x": map_response.map.origin.x,
                "y": map_response.map.origin.y,
                "theta": map_response.map.origin.theta,
            },
        }
        
        yaml_path = os.path.join(robot_dir, yaml_filename)
        with open(yaml_path, "w") as yaml_file:
            yaml.dump(map_metadata, yaml_file, default_flow_style=False)
        log_info(f"Saved {yaml_filename} locally")
        
        # Validate YAML file
        if not validate_yaml_file(yaml_path):
            log_error(f"YAML file validation failed for {robot_name}")
        
        # Save binary cursor file
        cursor_value = map_response.metadata.cursor
        bin_path = os.path.join(robot_dir, bin_filename)
        with open(bin_path, "wb") as binary_file:
            binary_file.write(struct.pack("<q", cursor_value))
        log_info(f"Saved {bin_filename} locally with cursor value: {cursor_value}")
        
        # Validate binary file
        if not validate_binary_file(bin_path):
            log_error(f"Binary file validation failed for {robot_name}")
        
        # Get and save waypoints
        locations_response = stub.GetLocations(kachaka_api_pb2.GetRequest())
        waypoints_path = os.path.join(robot_dir, waypoints_filename)
        with open(waypoints_path, "w") as f:
            for loc in locations_response.locations:
                print(f"Location: {loc}", file=f)
        log_info(f"Saved {waypoints_filename} locally with {len(locations_response.locations)} waypoints")
        
        # Transfer files to each target edge PC
        for edge_pc_id in target_edge_pcs:
            if edge_pc_id not in EDGE_PC_LOOKUP:
                log_error(f"Unknown edge PC ID: {edge_pc_id} for robot {robot_name}")
                continue
            
            edge_pc = EDGE_PC_LOOKUP[edge_pc_id].copy()  # Make a copy to avoid modifying the original
            edge_pc["password"] = edge_pc_passwords.get(edge_pc_id)
            
            # Initialize transfer status for this edge PC
            if edge_pc_id not in transfer_status["edge_pcs"]:
                transfer_status["edge_pcs"][edge_pc_id] = {
                    "connected": False,
                    "transfers": {}
                }
            
            if robot_name not in transfer_status["edge_pcs"][edge_pc_id]["transfers"]:
                transfer_status["edge_pcs"][edge_pc_id]["transfers"][robot_name] = {
                    "primary_dir_created": False,
                    "backup_dir_created": False,
                    "files_transferred": []
                }
            
            # Skip if we don't have a password for this edge PC
            if not edge_pc["password"]:
                log_error(f"No password available for {edge_pc['description']} ({edge_pc_id}), skipping transfer for {robot_name}")
                continue
            
            # Check SSH connection
            if not transfer_status["edge_pcs"][edge_pc_id].get("connected", False):
                connection_ok = check_ssh_connection_with_retry(edge_pc)
                transfer_status["edge_pcs"][edge_pc_id]["connected"] = connection_ok
                if not connection_ok:
                    log_error(f"Skipping transfer to {edge_pc['description']} due to connection failure")
                    continue
            
            # Transfer to primary location
            primary_dir = edge_pc["map_dir"]
            
            # Check if this is a system directory that might need special handling
            if primary_dir.startswith("/usr/") or primary_dir.startswith("/opt/"):
                system_dir_ok = ensure_system_directory_exists(edge_pc, primary_dir)
            if not system_dir_ok:
                    log_error(f"Failed to ensure system directory {primary_dir} exists on {edge_pc['description']}")
                    continue
            
            # Create robot directory in primary location
            primary_dir_created = create_remote_directory(robot_dir, edge_pc, primary_dir)
            transfer_status["edge_pcs"][edge_pc_id]["transfers"][robot_name]["primary_dir_created"] = primary_dir_created
            
            if primary_dir_created:
                # Transfer files to primary location
                files_to_transfer = [
                    (png_path, png_filename),
                    (jpg_path, jpg_filename),
                    (yaml_path, yaml_filename),
                    (bin_path, bin_filename),
                    (waypoints_path, waypoints_filename)
                ]
                
                primary_transfer_success = True
                for file_path, file_name in files_to_transfer:
                    success = transfer_files_scp(file_path, robot_dir, edge_pc, primary_dir)
                    primary_transfer_success &= success
                    if success:
                        transfer_status["edge_pcs"][edge_pc_id]["transfers"][robot_name].setdefault("files_transferred", []).append(file_name)
                
                if primary_transfer_success:
                    log_success(f"Successfully transferred all files for {robot_name} to primary location on {edge_pc['description']}")
                else:
                    log_error(f"Some files failed to transfer for {robot_name} to primary location on {edge_pc['description']}")
            else:
                log_error(f"Failed to create directory for {robot_name} in primary location on {edge_pc['description']}")
            
            # Transfer to backup location if configured
            backup_dir = edge_pc.get("backup_dir")
            if backup_dir:
                # Ensure home directory exists
                home_dir_ok = ensure_home_directory_exists(edge_pc, backup_dir)
                
                if home_dir_ok:
                    # Create robot directory in backup location
                    backup_dir_created = create_remote_directory(robot_dir, edge_pc, backup_dir)
                    transfer_status["edge_pcs"][edge_pc_id]["transfers"][robot_name]["backup_dir_created"] = backup_dir_created
                    
                    if backup_dir_created:
                        # Transfer files to backup location
                        backup_transfer_success = True
                        for file_path, file_name in files_to_transfer:
                            success = transfer_files_scp(file_path, robot_dir, edge_pc, backup_dir)
                            backup_transfer_success &= success
                        
                        if backup_transfer_success:
                            log_success(f"Successfully transferred all files for {robot_name} to backup location on {edge_pc['description']}")
                        else:
                            log_error(f"Some files failed to transfer for {robot_name} to backup location on {edge_pc['description']}")
                    else:
                        log_error(f"Failed to create directory for {robot_name} in backup location on {edge_pc['description']}")
                else:
                    log_error(f"Failed to ensure backup directory exists on {edge_pc['description']}, skipping backup transfer")
            
            # Update transfer status for this robot
            transfer_status["robots"][robot_name]["transfers"][edge_pc_id] = {
                "primary_success": primary_dir_created and primary_transfer_success,
                "backup_success": backup_dir and backup_dir_created and backup_transfer_success if backup_dir else None
            }
            
    except grpc.RpcError as e:
        log_error(f"gRPC error connecting to {robot_name} at {grpc_address}: {e}")
        return
    except Exception as e:
        log_error(f"Unexpected error processing {robot_name}: {str(e)}")
        return

def print_summary_report():
    """
    Prints a summary report of the transfer process.
    
    This function generates a detailed report showing:
    - Overall success status and percentage
    - Robot connection and map retrieval statistics
    - Edge PC connection statistics
    - Detailed status for each robot and edge PC
    - Robot-to-edge PC mapping
    - Location of the log file
    """
    print("\n" + "="*80)
    print("KACHAKA MAP TRANSFER SUMMARY REPORT")
    print("="*80)
    
    # Count successes and failures
    robot_count = len(transfer_status["robots"])
    connected_robots = sum(1 for r in transfer_status["robots"].values() if r.get("connected", False))
    maps_retrieved = sum(1 for r in transfer_status["robots"].values() if r.get("map_retrieved", False))
    
    edge_pc_count = len(transfer_status["edge_pcs"])
    connected_edge_pcs = sum(1 for e in transfer_status["edge_pcs"].values() if e.get("connected", False))
    
    # Calculate overall success percentage
    if robot_count > 0 and edge_pc_count > 0:
        success_percentage = (connected_robots / robot_count) * (connected_edge_pcs / edge_pc_count) * 100
    else:
        success_percentage = 0
    
    print(f"\nOverall Status: {'SUCCESS' if success_percentage > 80 else 'PARTIAL SUCCESS' if success_percentage > 0 else '❌ FAILURE'}")
    print(f"Success Rate: {success_percentage:.1f}%")
    print(f"\nRobots: {connected_robots}/{robot_count} connected, {maps_retrieved}/{robot_count} maps retrieved")
    print(f"Edge PCs: {connected_edge_pcs}/{edge_pc_count} connected")
    
    # Print robot details
    print("\nROBOT DETAILS:")
    print("-"*80)
    for robot_name, robot_status in transfer_status["robots"].items():
        connection_status = "Connected" if robot_status.get("connected", False) else "Failed to connect"
        map_status = "Retrieved" if robot_status.get("map_retrieved", False) else "Failed to retrieve"
        serial = robot_status.get("serial", "Unknown")
        
        print(f"\n{robot_name} (Serial: {serial}):")
        print(f"  Connection: {connection_status}")
        print(f"  Map Data: {map_status}")
        
        if "transfers" in robot_status and robot_status["transfers"]:
            print("  Transfers:")
            for edge_pc_id, transfer_result in robot_status["transfers"].items():
                edge_pc_desc = EDGE_PC_LOOKUP[edge_pc_id]["description"] if edge_pc_id in EDGE_PC_LOOKUP else edge_pc_id
                primary_status = "Success" if transfer_result.get("primary_success", False) else "Failed"
                
                backup_result = transfer_result.get("backup_success")
                if backup_result is None:
                    backup_status = "Not configured"
                else:
                    backup_status = "Success" if backup_result else "Failed"
                
                print(f"    {edge_pc_desc}:")
                print(f"      Primary: {primary_status}")
                print(f"      Backup: {backup_status}")
    
    # Print edge PC details
    print("\nEDGE PC DETAILS:")
    print("-"*80)
    for edge_pc_id, edge_pc_status in transfer_status["edge_pcs"].items():
        if edge_pc_id not in EDGE_PC_LOOKUP:
            continue
            
        edge_pc_desc = EDGE_PC_LOOKUP[edge_pc_id]["description"]
        connection_status = "Connected" if edge_pc_status.get("connected", False) else "Failed to connect"
        
        print(f"\n{edge_pc_desc} ({edge_pc_id}):")
        print(f"  Connection: {connection_status}")
        
        if "transfers" in edge_pc_status and edge_pc_status["transfers"]:
            print("  Received maps from:")
            for robot_name, transfer_result in edge_pc_status["transfers"].items():
                primary_created = "Created" if transfer_result.get("primary_dir_created", False) else "Failed"
                backup_created = "Created" if transfer_result.get("backup_dir_created", False) else "Failed" if "backup_dir_created" in transfer_result else "⚠️ Not attempted"
                
                files_transferred = transfer_result.get("files_transferred", [])
                file_count = len(files_transferred)
                
                print(f"    {robot_name}:")
                print(f"      Primary Directory: {primary_created}")
                print(f"      Backup Directory: {backup_created}")
                print(f"      Files Transferred: {file_count}/5 ({', '.join(files_transferred) if file_count <= 3 else str(file_count) + ' files'})")
    
    # Print robot-to-edge PC mapping
    print("\nROBOT TO EDGE PC MAPPING:")
    print("-"*80)
    for edge_pc_id, robots in transfer_status["robot_to_edge_mapping"].items():
        if edge_pc_id not in EDGE_PC_LOOKUP:
            continue
            
        edge_pc_desc = EDGE_PC_LOOKUP[edge_pc_id]["description"]
        print(f"\n{edge_pc_desc} ({edge_pc_id}) receives maps from:")
        for robot_name in robots:
            print(f"  • {robot_name}")
    
    print("\n" + "="*80)
    print(f"Log file: {LOG_FILE}")
    print("="*80 + "\n")

def main():
    """
    Main function to orchestrate map data retrieval and transfer.
    
    This function coordinates the entire map export process by parsing arguments,
    loading configuration, establishing connections, retrieving map data from robots,
    transferring files to edge PCs, and generating a summary report.
    
    It uses global variables to store configuration settings and creates threads
    to handle multiple robot connections simultaneously.
    
    Global Variables Modified:
        config: The loaded configuration
        KACHAKA_ROBOTS: List of robot configurations
        EDGE_PCS: List of edge PC configurations
        MAX_RETRIES: Maximum number of retry attempts
        RETRY_DELAY: Delay between retry attempts in seconds
        GRPC_PORT: Port number for gRPC connections
        TRANSFER_TIMEOUT: Timeout for file transfers in seconds
        EDGE_PC_LOOKUP: Dictionary mapping edge PC IDs to configurations
        EDGE_PC_DESC_TO_ID: Dictionary mapping edge PC descriptions to IDs
    """
    # Parse command line arguments
    args = parse_arguments()
    
    # Load configuration from specified file
    global config, KACHAKA_ROBOTS, EDGE_PCS, MAX_RETRIES, RETRY_DELAY, GRPC_PORT, TRANSFER_TIMEOUT, EDGE_PC_LOOKUP, EDGE_PC_DESC_TO_ID
    config = load_config(args.config)
    
    # Extract configuration values
    KACHAKA_ROBOTS = config['kachaka_robots']
    EDGE_PCS = config['edge_pcs']
    MAX_RETRIES = max(3, config['connection'].get('max_retries', 3))  # Ensure at least 3 retries
    RETRY_DELAY = config['connection'].get('retry_delay', 3)
    GRPC_PORT = config['connection'].get('grpc_port', 26400)
    TRANSFER_TIMEOUT = config['connection'].get('transfer_timeout', 30)
    
    # Update lookups
    EDGE_PC_LOOKUP = {edge_pc["id"]: edge_pc for edge_pc in EDGE_PCS}
    EDGE_PC_DESC_TO_ID = {edge_pc["description"]: edge_pc["id"] for edge_pc in EDGE_PCS}
    
    log_info("Starting Kachaka Map Exporter")
    log_info(f"Loaded configuration from {args.config}")
    log_info(f"Found {len(KACHAKA_ROBOTS)} robots and {len(EDGE_PCS)} edge PCs in configuration")
    
    # Collect all unique edge PC IDs that are targeted by robots
    targeted_edge_pc_ids = set()
    for robot in KACHAKA_ROBOTS:
        for edge_pc_id in robot.get("target_edge_pcs", []):
            targeted_edge_pc_ids.add(edge_pc_id)
    
    # Prompt for passwords and test connections for each edge PC in config order
    edge_pc_passwords = {}
    for edge_pc in EDGE_PCS:
        edge_pc_id = edge_pc["id"]
        if edge_pc_id not in targeted_edge_pc_ids:
            continue
            
        log_info(f"\nSetting up connection to {edge_pc['description']} ({edge_pc['username']}@{edge_pc['ip']})")
        password = getpass.getpass(f"Enter SSH password for {edge_pc['description']} ({edge_pc['username']}@{edge_pc['ip']}): ")
        
        # Test connection with exactly 3 attempts
        connected = False
        for attempt in range(1, 4):  # Always try up to 3 times, regardless of MAX_RETRIES
            log_info(f"Testing SSH connection to {edge_pc['description']}... Attempt {attempt}/3")
            
            # Test SSH connection
            try:
                ssh_command = [
                    "sshpass", "-p", password, "ssh",
                    "-o", "ConnectTimeout=5",  # Add timeout for faster failure
                    f"{edge_pc['username']}@{edge_pc['ip']}",
                    "echo 'SSH connection successful'"
                ]
                result = subprocess.run(ssh_command, check=True, capture_output=True, text=True, timeout=10)
                
                if "SSH connection successful" in result.stdout:
                    log_success(f"SSH connection to {edge_pc['description']} is successful")
                    connected = True
                    break
                else:
                    log_error(f"SSH connection to {edge_pc['description']} failed: Unexpected response")
            except subprocess.CalledProcessError as e:
                if "Permission denied" in (e.stderr or ""):
                    log_error(f"SSH connection to {edge_pc['description']} failed: Incorrect password")
                elif "Could not resolve hostname" in (e.stderr or ""):
                    log_error(f"SSH connection to {edge_pc['description']} failed: Hostname could not be resolved")
                else:
                    log_error(f"SSH connection to {edge_pc['description']} failed: {e.stderr}")
            except subprocess.TimeoutExpired:
                log_error(f"SSH connection to {edge_pc['description']} timed out: Server not responding")
            except Exception as e:
                log_error(f"Unexpected error connecting to {edge_pc['description']}: {str(e)}")
            
            # If we get here, the connection failed
            if attempt < 3:  # Always check against 3, not MAX_RETRIES
                # Ask if user wants to retry
                retry = input(f"Connection to {edge_pc['description']} failed. Do you want to try again? (Y/N): ")
                if retry.lower() != 'y':
                    log_info(f"Skipping further connection attempts to {edge_pc['description']}")
                    break
                
                # Get a new password
                password = getpass.getpass(f"Enter SSH password for {edge_pc['description']} ({edge_pc['username']}@{edge_pc['ip']}): ")
        
        # Store the password if connected successfully
        if connected:
            edge_pc_passwords[edge_pc_id] = password
            log_info(f"Edge PC {edge_pc['description']} is ready for transfers")
        else:
            log_warning(f"Skipping edge PC {edge_pc['description']} due to connection failure")
    
    # Check if at least one edge PC is connected
    if not edge_pc_passwords:
        log_error("No edge PCs are connected. Exiting.")
        return
    
    log_info(f"Successfully connected to {len(edge_pc_passwords)}/{len(targeted_edge_pc_ids)} targeted edge PCs")
    
    # Create threads for each robot
    threads = []
    for robot_config in KACHAKA_ROBOTS:
        thread = threading.Thread(
            target=get_map_data,
            args=(robot_config, edge_pc_passwords)
        )
        threads.append(thread)
        thread.start()
    
    # Wait for all threads to complete
    for thread in threads:
        thread.join()
    
    # Print summary report
    print_summary_report()
    
    log_info("Map transfer process completed")

if __name__ == "__main__":
    main()

