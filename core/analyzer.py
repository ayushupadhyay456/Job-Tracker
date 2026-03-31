from google import genai # New SDK
import os
import json
import yaml

class MailAnalyzer:
    def __init__(self, config_filename='filters.yaml'):
        current_dir = os.path.dirname(os.path.abspath(__file__))
        root_dir = os.path.dirname(current_dir)
        self.config_path = os.path.join(root_dir, config_filename)

        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise ValueError("GEMINI_API_KEY not found.")
        
        # Initializing the new client
        self.client = genai.Client(api_key=api_key)
        # Use 'gemini-2.0-flash' which is the technical name for Gemini 3 Flash
        self.model_id = "gemini-2.0-flash" 

        self.load_config()

    def load_config(self):
        if os.path.exists(self.config_path):
            with open(self.config_path, 'r') as f:
                self.config = yaml.safe_load(f)
        else:
            self.config = {"recruiter_keywords": ["interview", "hiring"]}

    def is_priority_subject(self, subject):
        subject_lower = subject.lower()
        keywords = self.config.get('recruiter_keywords', [])
        return any(kw.lower() in subject_lower for kw in keywords)

    def analyze(self, subject, body):
        is_priority = self.is_priority_subject(subject)

        prompt = f"""
        Determine if this email is a personalized reach-out from a recruiter.
        Exclude generic job board alerts or spam.
        
        Subject: {subject}
        Body: {body}
        
        Return ONLY a JSON object:
        {{
          "is_recruiter": bool,
          "confidence_score": int(0-100),
          "summary": "1-sentence summary",
          "keyword_match": {str(is_priority).lower()}
        }}
        """

        try:
            # New SDK syntax
            response = self.client.models.generate_content(
                model=self.model_id,
                contents=prompt
            )
            # The new SDK provides structured text
            clean_text = response.text.strip().removeprefix('```json').removesuffix('```').strip()
            return json.loads(clean_text)
        except Exception as e:
            return {
                "is_recruiter": is_priority,
                "confidence_score": 50 if is_priority else 0,
                "summary": f"API Error: {str(e)}",
                "keyword_match": is_priority
            }