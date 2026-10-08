"""Connection settings for the three data sources (override with environment variables)."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent

SQLITE_PATH = os.environ.get("SQLITE_PATH", str(ROOT / "data" / "f1.db"))

MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.environ.get("MONGO_DB", "f1_cars")

NEO4J_URI = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.environ.get("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD", "f1password")

MAPPING_XML = ROOT / "integration" / "mapping.xml"
GLOBAL_XSD = ROOT / "integration" / "global_schema.xsd"
