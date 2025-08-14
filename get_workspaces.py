import os
import smartsheet
from dotenv import load_dotenv

load_dotenv()
ss_token = os.getenv("SMARTSHEET_API")
ss_client = smartsheet.Smartsheet(ss_token)

# Fetch all workspaces
response = ss_client.Workspaces.list_workspaces()

# Print each workspace
for workspace in response.data:
    print(f"Name: {workspace.name} | ID: {workspace.id}")
