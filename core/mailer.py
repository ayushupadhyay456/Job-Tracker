import os
import sib_api_v3_sdk

def send_email(recruiter_email, recruiter_name, gist):
    configuration = sib_api_v3_sdk.Configuration()
    # Use environment variable for security
    configuration.api_key['api-key'] = os.getenv("BREVO_API_KEY")
    
    api_instance = sib_api_v3_sdk.TransactionalEmailsApi(sib_api_v3_sdk.ApiClient(configuration))

    # Draft a professional response based on the AI's gist
    html_content = f"""
    <p>Hi {recruiter_name},</p>
    <p>Thank you for reaching out regarding the opportunity. I read your message about '{gist}'.</p>
    <p>I am a Software Engineer with 3 years of experience specializing in Python, Flask, and Docker. I'd love to discuss this further.</p>
    <p>Best regards,<br>Ayush</p>
    """

    send_smtp_email = sib_api_v3_sdk.SendSmtpEmail(
        to=[{"email": recruiter_email}],
        sender={"name": "Ayush", "email": "your-verified-brevo-email@gmail.com"},
        subject="Re: Software Engineering Opportunity",
        html_content=html_content
    )

    try:
        api_instance.send_transac_email(send_smtp_email)
        return True
    except Exception as e:
        print(f"❌ Brevo Error: {e}")
        return False