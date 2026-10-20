"""Compatibility entrypoint; all business decisions belong to the employee."""
from app.classifier.semantic_folders import classify as classify_employee


def classify(text: str, *, employee=None, company_id: str = "default") -> str:
    return classify_employee(text=text, employee=employee, company_id=company_id)
