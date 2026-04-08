import asyncio
import os
import tempfile
import re
from email.message import EmailMessage
from email import message_from_bytes
import smtplib
from aiosmtpd.controller import Controller
import fitz  # PyMuPDF
from PIL import Image
import pytesseract
from pdf2image import convert_from_path
from presidio_analyzer import AnalyzerEngine, RecognizerResult
from presidio_anonymizer import AnonymizerEngine
from typing import List, Dict
from config import (LISTEN_HOST, LISTEN_PORT, REAL_SMTP_HOST, REAL_SMTP_PORT, REAL_SMTP_USER, REAL_SMTP_PASS)

analyzer = AnalyzerEngine()
anonymizer = AnonymizerEngine()

def custom_phi_recognizers(text: str) -> List[RecognizerResult]:
    results = []
    patterns = {
        "SSN": r"\b\d{3}[-.]?\d{2}[-.]?\d{4}\b",
        "PHONE": r"\b(\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b",
        "MRN": r"\bMRN[:\s]*[A-Z0-9-]{5,15}\b",
        "DOB": r"\b(DOB|Date of Birth)[:\s]*\d{1,2}[-/]\d{1,2}[-/]\d{2,4}\b",
    }
    for entity_type, pattern in patterns.items():
        for match in re.finditer(pattern, text, re.IGNORECASE):
            results.append(RecognizerResult(entity_type=entity_type, start=match.start(), end=match.end(), score=0.85))
    return results

def extract_text_from_pdf(pdf_path: str) -> str:
    doc = fitz.open(pdf_path)
    text = "".join(page.get_text("text") for page in doc)
    doc.close()
    return text

def extract_text_with_ocr(pdf_path: str) -> str:
    images = convert_from_path(pdf_path)
    return "".join(pytesseract.image_to_string(img) for img in images)

def analyze_text_for_phi(text: str) -> List[RecognizerResult]:
    if not text.strip():
        return []
    results = analyzer.analyze(text=text, language="en", score_threshold=0.6)
    results.extend(custom_phi_recognizers(text))
    return sorted(results, key=lambda x: x.start)

def scan_attachment(file_path: str) -> Dict:
    ext = os.path.splitext(file_path)[1].lower()
    try:
        if ext == ".pdf":
            text = extract_text_from_pdf(file_path)
            if not text.strip():  # scanned PDF
                text = extract_text_with_ocr(file_path)
            phi_found = analyze_text_for_phi(text)
        elif ext in [".jpg", ".jpeg", ".png", ".tiff"]:
            img = Image.open(file_path)
            text = pytesseract.image_to_string(img)
            phi_found = analyze_text_for_phi(text)
            
        else:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read()
            phi_found = analyze_text_for_phi(text)
        
        return {
            "phi_detected": len(phi_found) > 0,
            "details": [f"{r.entity_type}: {text[max(0, r.start-30):r.end+30]}" for r in phi_found[:3]]
        }
    except Exception as e:
        print(f"Scan error on {file_path}: {e}")
        return {"phi_detected": False, "details": []}

class PHIDLPHandler:
    async def handle_DATA(self, server, session, envelope):
        peer = session.peer
        data = envelope.content
        msg = message_from_bytes(data)

        print(f"\n[PHI Proxy] New email from {envelope.mail_from} to {envelope.rcpt_tos}")

        phi_detected = False
        phi_details = []
        temp_files = []

        try:
            for part in msg.walk():
                if part.get_content_maintype() == "multipart":
                    continue
                filename = part.get_filename()
                if not filename:
                    continue

                # Save attachment to temp file
                payload = part.get_payload(decode=True)
                if not payload:
                    continue

                with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(filename)[1]) as tmp:
                    tmp.write(payload)
                    temp_path = tmp.name
                temp_files.append(temp_path)

                result = scan_attachment(temp_path)
                if result["phi_detected"]:
                    phi_detected = True
                    phi_details.extend(result["details"])
                    print(f"   → PHI FOUND in attachment: {filename}")

        finally:
            # Cleanup temporary files
            for f in temp_files:
                try:
                    os.unlink(f)
                except:
                    pass

        if phi_detected:
            print("   → EMAIL BLOCKED: PHI detected in attachment(s)")
            return f"550 PHI detected in one or more attachments. Email blocked for compliance.\nDetails: {', '.join(phi_details)}"

        # No PHI → Forward to real mail server
        print("   → No PHI detected. Forwarding to real SMTP server...")
        try:
            with smtplib.SMTP(REAL_SMTP_HOST, REAL_SMTP_PORT) as smtp:
                smtp.starttls()
                smtp.login(REAL_SMTP_USER, REAL_SMTP_PASS)
                smtp.send_message(msg, from_addr=envelope.mail_from, to_addrs=envelope.rcpt_tos)
            print("   → Email successfully forwarded!")
            return "250 OK - Message accepted (PHI scan passed)"
        except Exception as e:
            print(f"   → Forward error: {e}")
            return f"550 Forward failed: {str(e)}"


# ====================== START THE PROXY ======================
if __name__ == "__main__":
    handler = PHIDLPHandler()
    controller = Controller(handler, hostname=LISTEN_HOST, port=LISTEN_PORT)
    
    print(f"🚀 PHI Email Proxy started on {LISTEN_HOST}:{LISTEN_PORT}")
    print("   Configure your email client outgoing SMTP to:")
    print(f"   Server: 127.0.0.1    Port: 2525    Security: None    Authentication: None")
    print("   Press Ctrl + C to stop the proxy\n")

    controller.start()
    try:
        asyncio.get_event_loop().run_forever()
    except KeyboardInterrupt:
        controller.stop()
        print("\nPHI Proxy stopped.")