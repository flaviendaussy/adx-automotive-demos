# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
import argparse
from asammdf import MDF
from asammdf.blocks import v4_constants as v4c
import csv
from datetime import datetime, timedelta
import gzip
import json
import os
import pandas as pd
from   pathlib import Path
import time 
import multiprocessing as mp
import numpy as np
import uuid
import pyarrow as pa
import pyarrow.parquet as pq


def dumpSignals(mdf):
    '''Iterates over all signals and prints them to the console'''    

    for counter, signal in enumerate(mdf.iter_channels()):
        channel_group_acq_name, acq_source_name, acq_source_path = getSource(mdf, signal)
        print(f"{signal.source.name}, {channel_group_acq_name}, {acq_source_name}, {acq_source_path}, {signal.name}, {signal.unit}, {v4c.SOURCE_TYPE_TO_STRING[signal.source.source_type]}, {v4c.BUS_TYPE_TO_STRING[signal.source.bus_type]}, {signal.group_index}, {signal.channel_index}")

    return counter

def writeMetadata(basename, mdf, uuid, target, numberOfChunks):
    '''Creates a JSON metadata file containing the signals description for the MDF-4 file'''

    with open(os.path.join(target, f"{basename}-{uuid}.metadata.json"), 'w') as metadataFile:
        
        print(f"Writing metadata file {basename}-{uuid}")

        metadata = {
            "name": basename,
            "source_uuid": str(uuid),
            "preparation_startDate": str(datetime.utcnow()),
            "signals": [],
            "comments": mdf.header.comment,
            "numberOfChunks": numberOfChunks
        }


        for signal in mdf.iter_channels():

            channel_group_acq_name, acq_source_name, acq_source_path = getSource(mdf, signal)

            metadata["signals"].append(
                {
                    "name": signal.name,
                    "unit": signal.unit,
                    "comment": signal.comment,
                    "group_index": signal.group_index,
                    "channel_index": signal.channel_index,
                    "channel_group_acq_name": channel_group_acq_name,
                    "acq_source_name": acq_source_name,
                    "acq_source_path": acq_source_path,
                    "source" : signal.source.name,
                    "source_type": v4c.SOURCE_TYPE_TO_STRING[signal.source.source_type],
                    "bus_type": v4c.BUS_TYPE_TO_STRING[signal.source.bus_type],
                }          
            )

        metadataFile.write(json.dumps(metadata))
       
    metadataFile.close()

def writeParquet(basename, mdf, uuid, target):       
    '''
        Writes the MDF-4 file to a parquet file
    '''

    targetdir = os.path.join(target, f"{basename}-{uuid}")

    # Create a pool of worker processes
    pool = mp.Pool(mp.cpu_count())
        
    # Iterate over the signals contained in the MDF-4 file
    result = []
    for counter, signal in enumerate(mdf.iter_channels(copy_master=False)):                       
        
        channel_group_acq_name, acq_source_name, acq_source_path = getSource(mdf, signal)
        # Apply the processSignal function to each signal asynchronously
        #processSignal(counter, channel_group_acq_name, acq_source_name, acq_source_path, signal, uuid, targetdir)        
        result.append(pool.apply_async(processSignal, args=(counter, channel_group_acq_name, acq_source_name, acq_source_path, signal, uuid, targetdir)))
    
    # Wait for all processes to finish
    for r in result:
        r.wait()
      
    return counter


def processSignal(counter, channel_group_acq_name, acq_source_name, acq_source_path, signal, uuid, targetdir):
    '''
        Creates a parquet export with the structure that we will import into ADX.
        There are three important pieces of information for time analysis of automotive signals
            - The signal itself, which might be a record, numeric or string
            - The frame, which might contain multiple signals
            - The bus (some signals might be avaialble on multiple buses such as CAN and LIN)

        Additional information that is relevant for analytis:
            - The source ECU, as some analysis are related to specific Electronic Control Units.
            - The type of BUS, as some analysis are specific to CAN, LIN or ETH.

    '''
    start_signal_time = time.time()
    print(f"Processing signal {counter}: {signal.name} with type {signal.samples.dtype}")   

    try:
        numericSignals, stringSignals = extractSignalsByType(signal)
        
        table = pa.table (
            {                   
                "source_uuid": np.full(len(signal.timestamps), str(uuid), dtype=object),
                "name": np.full(len(signal.timestamps), signal.name, dtype=object),
                "unit": np.full(len(signal.timestamps), signal.unit, dtype=object),
                "timestamp": signal.timestamps,
                "value": numericSignals,
                "value_string": stringSignals,
                "source": np.full(len(signal.timestamps), signal.source.name, dtype=object),
                "channel_group_acq_name": np.full(len(signal.timestamps), channel_group_acq_name, dtype=object),
                "acq_source_name": np.full(len(signal.timestamps), acq_source_name, dtype=object),
                "acq_source_path": np.full(len(signal.timestamps), acq_source_path, dtype=object),
                "source_type": np.full(len(signal.timestamps), v4c.SOURCE_TYPE_TO_STRING[signal.source.source_type], dtype=object),
                "bus_type": np.full(len(signal.timestamps), v4c.BUS_TYPE_TO_STRING[signal.source.bus_type], dtype=object)
            }
        ) 
        pq.write_to_dataset(table, root_path=targetdir)

        del table
        
    except Exception as e:
        print(f"Signal {counter}: {signal.name} with {len(signal.timestamps)} failed: {e}")    

    end_signal_time = time.time() - start_signal_time
    print(f"Signal {counter}: {signal.name} with {len(signal.timestamps)} entries took {end_signal_time}")


def getSource(mdf, signal):
    '''
        Extracts the source information from the MDF-4 file for a given signal
    '''

    try: 
        channel_group_acq_name = mdf.groups[signal.group_index].channel_group.acq_name
    except:
        channel_group_acq_name = ""

    try: 
        acq_source_name = mdf.groups[signal.group_index].channel_group.acq_source.name
    except:
        acq_source_name = ""

    try:
        acq_source_path = mdf.groups[signal.group_index].channel_group.acq_source.path
    except:
        acq_source_path = ""

    return channel_group_acq_name, acq_source_name, acq_source_path

def extractSignalsByType(signal):
    '''
        Extracts the signals from the MDF-4 file and converts them to a numeric or string representation
        Takes into consideration numbers, strings and records (rendered as a string) 
    '''   

    if np.issubdtype(signal.samples.dtype, np.record):
        numericSignals = np.full(len(signal.timestamps), dtype=np.double, fill_value=0)      
        stringSignals = [record.pprint() for record in signal.samples]

    elif np.issubdtype(signal.samples.dtype, np.number):
        numericSignals = signal.samples.astype(np.double)
        stringSignals = np.empty(len(signal.timestamps), dtype=str) 

    else:
        numericSignals = np.full(len(signal.timestamps), dtype=np.double, fill_value=0)            
        stringSignals = signal.samples.astype(str)
    return numericSignals,stringSignals

def writeCsv(basename, mdf, uuid, target):
    '''    
        Writes a gzipped CSV file using the uuid as name
        It will write a file for each individual signal
    '''

    targetdir = os.path.join(target, f"{basename}-{uuid}")

    # Iterate over the signals contained in the MDF-4 file
    for counter, signal in enumerate(mdf.iter_channels(copy_master=False)):                     
        # Apply the processSignal function to each signal asynchronously
    
        channel_group_acq_name, acq_source_name, acq_source_path = getSource(mdf, signal)
        processSignalCSV(counter, channel_group_acq_name, acq_source_name, acq_source_path, signal, uuid, targetdir, basename)
        
    return counter

def processSignalCSV(counter, channel_group_acq_name, acq_source_name, acq_source_path, signal, uuid, targetdir, basename):
    start_signal_time = time.time()
    print(f"Processing signal {counter}: {signal.name} with type {signal.samples.dtype}")   
    numericSignals, stringSignals = extractSignalsByType(signal)

    os.makedirs(targetdir, exist_ok=True)


    # open the file in the write mode
    with gzip.open(os.path.join(targetdir, f"{uuid}-{counter}.csv.gz"), 'wt') as csvFile:

        writer = csv.writer(csvFile)
        writer.writerow(["source_uuid", "name", "unit", "timestamp", "value", "value_string", "source", "channel_group_acq_name", "acq_source_name", "acq_source_path", "source_type", "bus_type"])                
                        
        # Iterate on the entries for the signal

        for indx in range(0, len(signal.timestamps)):

            try:
                numericValue = float(signal.samples[indx])
            except:
                numericValue = "",

            writer.writerow(
                [
                    str(uuid),
                    signal.name, 
                    signal.unit, 
                    signal.timestamps[indx],
                    numericSignals[indx],
                    stringSignals[indx],
                    signal.source.name,
                    channel_group_acq_name,
                    acq_source_name,
                    acq_source_path,                    
                    v4c.SOURCE_TYPE_TO_STRING[signal.source.source_type],
                    v4c.BUS_TYPE_TO_STRING[signal.source.bus_type],
                ]
            )

    csvFile.close()
    
    end_signal_time = time.time() - start_signal_time
    print(f"Signal {counter}: {signal.name} with {len(signal.timestamps)} entries took {end_signal_time}")

def processFile(filename):
    '''Processes a single MDF file.'''

    start_time = time.time()
    mdf = MDF(filename)
    numberOfChunks = 0

    # If the argument is dump, show the signals
    if (args.dump):
       numberOfChunks = dumpSignals(mdf)
        
    # Otherwise export
    else:        
        basename = Path(filename).stem
        file_uuid = uuid.uuid4()  
        numberOfChunks = 0

        # Use the right method based on the format
        if (args.exportFormat == "parquet"):         
            numberOfChunks = writeParquet(basename, mdf, file_uuid, args.target)
        elif (args.exportFormat == "csv"):        
            numberOfChunks = writeCsv(basename, mdf, file_uuid, args.target)        
        else:
            print("No export Format selected, use --format with parquet or csv")
        
        # Write an additional metadata file with all the signal information
        writeMetadata(basename, mdf, file_uuid, args.target, numberOfChunks)

    end_time = time.time() - start_time
    print (f"Processing {filename} took {end_time} and has {numberOfChunks} signals")

def processDirectory(directoryname):
    '''Processes a complete directoy containing several MDF-4 files.'''
    for path in os.listdir(directoryname):
        if os.path.isfile(os.path.join(directoryname, path)):
            processFile(os.path.join(directoryname, path))

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Process a single MDF-4 or directory with MDF-4 files into CSV or Parquet Files.")
    parser.add_argument("-f", "--file", dest="file", help="Path to a single MDF-4 file.")
    parser.add_argument("-d", "--directory", dest="directory", help="Path to a directory with MDF-4 files.")
    parser.add_argument("-t", "--target", dest="target", default=".", help="Location where the processed files will be stored.")
    parser.add_argument("--dump", dest="dump", action="store_true", help="Shows the signals contained in the file. No export will be made.")
    parser.add_argument("--format", dest="exportFormat", default="parquet", help="Use csv or parquet to select your export format. Default is parquet")
    args = parser.parse_args()

    if(args.file):
        processFile(args.file)
    elif(args.directory):
        processDirectory(args.directory)
