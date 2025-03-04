#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Multi-Map Exporter a python script for Kachaka Fleet Manager configuration on edge PC.

Author: Jimmy Majumder
Date: 2025-02-18
Copyright: QibiTech Inc.

Description:
This script exports maps from multiple Kachaka robots to a centralized edge PC. It facilitates fleet management 
by automating map data extraction (PNG, YAML metadata, binary cursor files) 
and ensuring structured storage.

Functionality:
- Establishes gRPC connections with multiple Kachaka robots.
- Extracts and saves map data (PNG, YAML metadata, binary cursor files).
- Transfers map files securely to an edge PC using SCP.
- Ensures structured storage of maps per robot to facilitate fleet operations.
- Logs execution details to a log file for debugging and audit purposes.

System Requirements:
- sshpass: Required for non-interactive SCP file transfers.  Consider SSH key-based 
  authentication for enhanced security in production environments.
- gRPC:  Ensure the gRPC service is running and accessible on each Kachaka robot.
- SSH access: The edge PC must permit SSH connections from the executing system.

Security Considerations:
- The script prompts for an SSH password at runtime; it is not stored in plaintext.
- It is recommended to use SSH key-based authentication for production deployments.
- Ensure that the edge PC is secure and has proper access controls in place.

Usage:
Execute the script: python3 multi_map_exporter.py

***Note:
Before running the script, please ensure you have read the following documentation:
README - Setup & Procedure: 
https://bitbucket.org/qibitech/kachaka-api/src/b2bfce371dcf0a36132c082d348521c0085e94ba/python/demos/README_SMART_SPEAKER.md?at=feature%2Fmulti_map_setup_fleet_manager

Prerequisites:
1. git clone git@bitbucket.org:qibitech/kachaka-api.git # clone the repository
2. python3 -m venv venv # do this if you want to create a virtual environment
3. source venv/bin/activate # do this if you created a virtual environment
4. cd kachaka-api/python/demos # go to the directory where the script is located
5. pip install -r requirements.txt
6. cd grpc_samples
7. python3 -m grpc_tools.protoc -I../../../protos --python_out=. --pyi_out=. --grpc_python_out=. ../../../protos/kachaka-api.proto # make sure where the proto file is located
8. python3 multi_map_exporter.py # run the script
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
from datetime import datetime

# Configuration
GRPC_CHANNEL_ADDRESSES = [
    "192.168.2.77:26400",  # Kachaka_Sales01 (Replace with actual IP:port)
    # "192.168.2.78:26400",  # Kachaka_11 (Replace with actual IP:port)
    # "192.168.2.79:26400",  # Kachaka_12 (Replace with actual IP:port)
]

# EDGE_PC_DESCRIPTION:
#     "The Edge PC is a centralized PC"
#     "In QibiTech, the Edge PC ensures synchronized control and fleet management of different types of robots, "
#     "It functions as a bridge to the HATS system, providing seamless communication between fleet manager, kachaka robot and HATS UI. "
#     "It facilitating real-time monitoring, decision-making, data-driven feedback monitoring."

EDGE_PC_IP = "192.168.2.183" # default IP, change as needed
EDGE_PC_USERNAME = "qtmember" # default username , change as needed
EDGE_PC_MAP_DIR = "/usr/local/share/hats_sdk/map/" # default directory, change as needed

# Logging Setup
LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, f"kachaka_map_transfer_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
logging.basicConfig(filename=LOG_FILE, level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def _log_message(level, message):
    """
    Logs messages to both console and log file.

    Args:
        level (int): Logging level (e.g., logging.INFO, logging.ERROR).
        message (str): The message to log.
    """
    print(message)  # Always print to console
    logging.log(level, message)

def log_info(message):
    """Logs an informational message."""
    _log_message(logging.INFO, message)

def log_error(message):
    """Logs an error message."""
    _log_message(logging.ERROR, f"ERROR: {message}")  # Added ERROR prefix

def check_ssh_connection(password):
    """
    Verifies SSH connectivity to the edge PC.

    Args:
        password (str): SSH password.

    Returns:
        bool: True if connection is successful, False otherwise.
    """
    try:
        ssh_command = [
            "sshpass", "-p", password, "ssh",
            f"{EDGE_PC_USERNAME}@{EDGE_PC_IP}",
            "echo 'SSH connection successful'"  # A simple command to test connectivity
        ]
        subprocess.run(ssh_command, check=True, capture_output=True, text=True)
        log_info("SSH connection successful")
        return True
    except subprocess.CalledProcessError as e:
        log_error(f"SSH connection failed: {e.stderr}")
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

def create_remote_directory(robot_id, password):
    """
    Creates a directory on the edge PC for storing map data.

    Args:
        robot_id (str): The robot's serial number (used as directory name).
        password (str): SSH password.
    """
    try:
        ssh_command = [
            "sshpass", "-p", password, "ssh",
            f"{EDGE_PC_USERNAME}@{EDGE_PC_IP}",
            f"mkdir -p {EDGE_PC_MAP_DIR}{robot_id}"
        ]
        subprocess.run(ssh_command, check=True, capture_output=True, text=True)
        log_info(f"Successfully created directory {EDGE_PC_MAP_DIR}{robot_id} on edge PC")
    except subprocess.CalledProcessError as e:
        log_error(f"Error creating directory {EDGE_PC_MAP_DIR}{robot_id} on edge PC: {e.stderr}")
        raise  # Re-raise exception to stop further processing if directory creation fails

def transfer_files_scp(source_file, robot_id, destination, password):
    """
    Transfers files to the edge PC using SCP.

    Args:
        source_file (str): Path to the file to transfer.
        robot_id (str): The robot's serial number.
        destination (str): Destination directory on the edge PC.
        password (str): SSH password.
    """
    try:
        robot_dir = f"{destination}{robot_id}/"
        scp_command = [
            "sshpass", "-p", password, "scp",
            source_file,
            f"{EDGE_PC_USERNAME}@{EDGE_PC_IP}:{robot_dir}"
        ]
        subprocess.run(scp_command, check=True, capture_output=True, text=True)
        log_info(f"Successfully transferred {source_file} to {robot_dir}")
    except subprocess.CalledProcessError as e:
        log_error(f"Error transferring {source_file} to {robot_dir}: {e.stderr}")
        raise  # Re-raise exception to stop further processing if file transfer fails

def get_map_data(grpc_channel_address, password):
    """
    Retrieves map data from a Kachaka robot via gRPC.

    Args:
        grpc_channel_address (str): The IP address and port of the Kachaka robot.
        password (str): SSH password.
    """
    try:
        stub = KachakaApiStub(grpc.insecure_channel(grpc_channel_address))

        serial_number_response = stub.GetRobotSerialNumber(kachaka_api_pb2.GetRequest())
        robot_serial_number = serial_number_response.serial_number
        log_info(f"---------- serial number ({grpc_channel_address}): {robot_serial_number} ----------")

        current_map_id_response = stub.GetCurrentMapId(kachaka_api_pb2.GetRequest())
        map_id = current_map_id_response.id
        log_info(f"---------- map id ({grpc_channel_address}): {map_id} ----------")

        response = stub.GetRobotVersion(kachaka_api_pb2.GetRequest())
        log_info(f"---------- robot version ({grpc_channel_address}) ----------")
        log_info(str(response))

        response = stub.GetPngMap(kachaka_api_pb2.GetRequest())
        log_info(f"---------- Map ({grpc_channel_address}) ----------")
        log_info(str(response))

        map_name = sanitize_filename(response.map.name)
        filename_prefix = f"{map_name}"
        png_filename = f"{filename_prefix}.png"
        yaml_filename = f"{filename_prefix}_metadata.yaml"
        bin_filename = f"{filename_prefix}_metadata.bin"

        # Create a directory for this robot locally
        robot_dir = f"Kachaka_{robot_serial_number}"
        os.makedirs(robot_dir, exist_ok=True)
        log_info(f"Successfully created local directory: {robot_dir}")

        # Save files locally
        with open(os.path.join(robot_dir, png_filename), "wb") as binary_file:
            binary_file.write(response.map.data)
        log_info(f"Successfully saved {png_filename} locally.")

        map_metadata = {
            "name": response.map.name,
            "resolution": response.map.resolution,
            "width": response.map.width,
            "height": response.map.height,
            "origin": {
                "x": response.map.origin.x,
                "y": response.map.origin.y,
                "theta": response.map.origin.theta,
            },
        }

        log_info(f"Extracted Map Metadata ({grpc_channel_address}): {map_metadata}")

        with open(os.path.join(robot_dir, yaml_filename), "w") as yaml_file:
            yaml.dump(map_metadata, yaml_file, default_flow_style=False)
        log_info(f"Successfully saved {yaml_filename} locally.")

        cursor_value = response.metadata.cursor
        with open(os.path.join(robot_dir, bin_filename), "wb") as binary_file:
            binary_file.write(struct.pack("<q", cursor_value))
        log_info(f"Successfully saved {bin_filename} locally.")

        with open(os.path.join(robot_dir, bin_filename), "rb") as binary_file:
            binary_data = binary_file.read(8)
            cursor_value_read = struct.unpack("<q", binary_data)[0]
            log_info(f"Read Cursor Value ({grpc_channel_address}): {cursor_value_read}")

        # Create remote directory and transfer files
        create_remote_directory(robot_dir, password)
        transfer_files_scp(os.path.join(robot_dir, png_filename), robot_dir, EDGE_PC_MAP_DIR, password)
        transfer_files_scp(os.path.join(robot_dir, yaml_filename), robot_dir, EDGE_PC_MAP_DIR, password)
        transfer_files_scp(os.path.join(robot_dir, bin_filename), robot_dir, EDGE_PC_MAP_DIR, password)

    except grpc.RpcError as e:
        log_error(f"Error connecting to {grpc_channel_address}: {e}")
    except Exception as e:
        log_error(f"An unexpected error occurred: {e}")

def main():
    """Main function to orchestrate map data retrieval and transfer."""
    # Prompt for password once at the beginning
    password = getpass.getpass("Enter your SSH password: ")

    if not check_ssh_connection(password):
        log_error("Failed to establish SSH connection. Exiting.")
        return

    threads = []
    for address in GRPC_CHANNEL_ADDRESSES:
        thread = threading.Thread(target=get_map_data, args=(address, password))
        threads.append(thread)
        thread.start()

    for thread in threads:
        thread.join()

    log_info("Map transfer process completed")

if __name__ == "__main__":
    main()
