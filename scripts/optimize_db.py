import os
import sys

# Add parent directory to path
sys.path.append(os.getcwd())

from pymongo import MongoClient, ASCENDING, DESCENDING
from config import Config

def create_optimization_indexes():
    client = MongoClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]
    
    print("--- Creating Indexes for Performance Optimization ---")
    
    # 1. Collection: leads
    print("\n[Leads]")
    # For filtering by executive (CRM list and SLA monitor)
    print("Creating index on 'ejecutivo_asignado'...")
    db["leads"].create_index([("ejecutivo_asignado", ASCENDING)])
    print("Creating index on 'prospecto.ejecutivo'...")
    db["leads"].create_index([("prospecto.ejecutivo", ASCENDING)])
    
    # For filtering by stage (CRM list and SLA monitor)
    print("Creating index on 'pipeline_stage'...")
    db["leads"].create_index([("pipeline_stage", ASCENDING)])
    print("Creating index on 'stage'...")
    db["leads"].create_index([("stage", ASCENDING)])
    
    # For phone lookups (CRM list uses regex)
    print("Creating index on 'phone'...")
    db["leads"].create_index([("phone", ASCENDING)])

    # 2. Collection: crm_events
    print("\n[CRM Events]")
    # SLA monitor and lead detail use these heavily
    print("Creating index on 'phone' and 'type'...")
    db["crm_events"].create_index([("phone", ASCENDING), ("type", ASCENDING)])
    print("Creating index on 'timestamp' for sorting...")
    db["crm_events"].create_index([("timestamp", DESCENDING)])

    # 3. Collection: crm_sla_warnings
    print("\n[SLA Warnings]")
    print("Creating index on 'phone'...")
    db["crm_sla_warnings"].create_index([("phone", ASCENDING)])

    # 4. Collection: universo_obelix
    print("\n[Universo Obelix]")
    print("Creating index on 'ejecutivo'...")
    db["universo_obelix"].create_index([("ejecutivo", ASCENDING)])
    
    print("\n--- All optimization indexes created successfully! ---")

if __name__ == "__main__":
    create_optimization_indexes()
