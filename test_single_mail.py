import os
from dotenv import load_dotenv
from core.analyzer import MailAnalyzer
import json

# Load your GEMINI_API_KEY from .env
load_dotenv()

def test_specific_email():
    # Initialize the analyzer (it will look for filters.yaml in root)
    analyzer = MailAnalyzer()

    print("--- Testing Single Email Analysis (Gemini 3 Flash) ---")
    
    # PASTE THE DETAILS OF THE EMAIL YOU SENT HERE:
    test_subject = "Urgent: Technical Interview Scheduling - Software Engineer"
    test_body = """
    Hi Ayush, 
    
    We reviewed your profile for the Software Engineer position and would 
    like to schedule a technical round this week. 
    
    Are you available this Thursday?
    """

    # Run the analysis
    result = analyzer.analyze(test_subject, test_body)

    # Output the results
    print("\n[ANALYSIS RESULT]")
    print(json.dumps(result, indent=4))

    if result.get("is_recruiter"):
        print("\n✅ SUCCESS: Identified as a recruiter email.")
    else:
        print("\n❌ FAILED: Not identified as a recruiter email.")

if __name__ == "__main__":
    test_specific_email()