#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
This is a test script for exporting maps from multiple Kachaka robots using gRPC.

Author: Jimmy Majumder
Date: 2025-02-20
Copyright: QibiTech Inc.

"""

import sys
import grpc
import kachaka_api_pb2
from kachaka_api_pb2_grpc import KachakaApiStub
import yaml
import struct
import threading
import re
import os

# Define the list of gRPC channel addresses for your Kachaka robots
grpc_channel_addresses = [
    "192.168.2.77:26400",  # Replace with your robot's address
    # "192.168.2.78:26400",  # Add more addresses as needed
    # "192.168.2.79:26400",
]

def sanitize_filename(filename):
    """Sanitizes a string to be a safe filename."""
    # Replace invalid characters with underscores
    filename = re.sub(r'[^\w\-_\.]', '_', filename)
    return filename

def get_map_data(grpc_channel_address):
    try:
        stub = KachakaApiStub(grpc.insecure_channel(grpc_channel_address))

        # Get robot serial number
        serial_number_response = stub.GetRobotSerialNumber(kachaka_api_pb2.GetRequest())
        robot_serial_number = serial_number_response.serial_number
        print(f"---------- serial number ({grpc_channel_address}): {robot_serial_number} ----------")

        # Create a folder with the robot's serial number
        folder_name = sanitize_filename(robot_serial_number)
        os.makedirs(folder_name, exist_ok=True)

        # Get current map ID
        current_map_id_response = stub.GetCurrentMapId(kachaka_api_pb2.GetRequest())
        map_id = current_map_id_response.id
        print(f"---------- map id ({grpc_channel_address}): {map_id} ----------")

        response = stub.GetRobotVersion(kachaka_api_pb2.GetRequest())
        print(f"---------- robot version ({grpc_channel_address}) ----------")
        print(response)

        response = stub.GetPngMap(kachaka_api_pb2.GetRequest())
        print(f"---------- Map ({grpc_channel_address}) ----------")
        print(response)

        # Extract the map name and sanitize it
        map_name = sanitize_filename(response.map.name)

        # Construct the filename using only the map_name
        filename_prefix = f"{map_name}"
        png_filename = os.path.join(folder_name, f"{filename_prefix}.png")
        yaml_filename = os.path.join(folder_name, f"{filename_prefix}_metadata.yaml")
        bin_filename = os.path.join(folder_name, f"{filename_prefix}_metadata.bin")

        # Save the map image data
        with open(png_filename, "wb") as binary_file:
            binary_file.write(response.map.data)

        # Extract map metadata
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

        # Print extracted map metadata
        print(f"Extracted Map Metadata ({grpc_channel_address}):", map_metadata)

        # Save map metadata to YAML file
        with open(yaml_filename, "w") as yaml_file:
            yaml.dump(map_metadata, yaml_file, default_flow_style=False)

        # Save metadata (cursor) to binary file
        cursor_value = response.metadata.cursor
        with open(bin_filename, "wb") as binary_file:
            # Pack the sfixed64 value into bytes " '<q' for little-endian sfixed64"
            binary_file.write(struct.pack("<q", cursor_value))

        # Read metadata (cursor) from binary file
        with open(bin_filename, "rb") as binary_file:
            binary_data = binary_file.read(8)
            cursor_value_read = struct.unpack("<q", binary_data)[0]
            print(f"Read Cursor Value ({grpc_channel_address}):", cursor_value_read)

    except grpc.RpcError as e:
        print(f"Error connecting to {grpc_channel_address}: {e}")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")  # catch other errors

def main():
    threads = []
    for address in grpc_channel_addresses:
        thread = threading.Thread(target=get_map_data, args=(address,))
        threads.append(thread)
        thread.start()

    for thread in threads:
        thread.join()  # Wait for all threads to complete

if __name__ == "__main__":
    main()
