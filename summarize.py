#!/usr/bin/env python3

"""
Meeting summarization script using Ollama
Takes transcription text and generates meeting notes, summaries, and action items
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

try:
    import requests
except ImportError:
    print("Error: requests library not installed", file=sys.stderr)
    print("Install with: pip install requests", file=sys.stderr)
    sys.exit(1)


DEFAULT_OLLAMA_URL = "http://localhost:11434"

SUMMARY_PROMPT = """You are an assistant that writes concise meeting notes from transcripts.

Produce the following sections using markdown headings:

## Summary
2-3 sentences covering what the meeting was about and what was concluded.

## Discussion
Bullet points of the main topics covered. Be specific, not generic.

## Decisions
Key decisions or conclusions reached. Omit this section if none were made.

## Action Items
A markdown checklist of concrete next steps that were explicitly agreed on, with owner and deadline if mentioned. Only include items that were clearly committed to — not vague intentions or possibilities. Omit this section entirely if there are no real action items.

## Participants
Names or roles of identifiable speakers, if mentioned.

Transcription:
{transcription}

Use markdown headings and bullet points. Do not wrap your response in a code block."""


def query_ollama(prompt: str, model: str = "llama3.1:8b", ollama_url: str = DEFAULT_OLLAMA_URL) -> str:
    """
    Query Ollama API for text generation

    Args:
        prompt: The prompt to send
        model: Model name to use
        ollama_url: Ollama API URL

    Returns:
        Generated text response
    """
    try:
        response = requests.post(
            f"{ollama_url}/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "stream": False
            },
            timeout=300  # 5 minute timeout
        )
        response.raise_for_status()
        return response.json()["response"]
    except requests.exceptions.RequestException as e:
        print(f"Error querying Ollama: {e}", file=sys.stderr)
        sys.exit(1)


def query_openai(prompt: str, model: str) -> str:
    """Generate a summary with the OpenAI Responses API without storing a key in this project."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Export it in the terminal before running HushNote."
        )

    max_retries = int(os.environ.get("OPENAI_MAX_RETRIES", "3"))
    delay = float(os.environ.get("OPENAI_RETRY_INITIAL_SECONDS", "30"))
    for attempt in range(max_retries + 1):
        try:
            response = requests.post(
                "https://api.openai.com/v1/responses",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "input": prompt,
                    "reasoning": {"effort": os.environ.get("OPENAI_REASONING_EFFORT", "none")},
                    "max_output_tokens": 1200,
                },
                timeout=300,
            )
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"Error querying OpenAI: {e}") from e
        if response.status_code == 429:
            error = _openai_error_details(response)
            if error.get("code") == "insufficient_quota":
                raise RuntimeError(_format_openai_error(response, error))
            if attempt < max_retries:
                retry_after = _retry_after_seconds(response) or delay
                print(
                    f"OpenAI rate limited; retrying in {retry_after:.0f}s "
                    f"(attempt {attempt + 1}/{max_retries})",
                    file=sys.stderr,
                )
                time.sleep(retry_after)
                delay = min(delay * 2, 300)
                continue
            raise RuntimeError(_format_openai_error(response, error))
        try:
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            error = _openai_error_details(response)
            raise RuntimeError(_format_openai_error(response, error)) from e
        body = response.json()
        if body.get("output_text"):
            return body["output_text"]

        parts = []
        for output in body.get("output", []):
            for content in output.get("content", []):
                if content.get("type") == "output_text":
                    parts.append(content.get("text", ""))
        if parts:
            return "\n".join(parts)
        raise RuntimeError("OpenAI returned no text output")
    raise RuntimeError("OpenAI request failed after retries")


def _openai_error_details(response: requests.Response) -> dict:
    try:
        body = response.json()
    except ValueError:
        body = {}
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        error = {"message": response.text[:500]}
    return error


def _format_openai_error(response: requests.Response, error: dict) -> str:
    request_id = response.headers.get("x-request-id", "")
    reset_tokens = response.headers.get("x-ratelimit-reset-tokens", "")
    reset_requests = response.headers.get("x-ratelimit-reset-requests", "")
    details = [
        f"OpenAI API error {response.status_code}",
        f"type={error.get('type', '')}",
        f"code={error.get('code', '')}",
        f"message={error.get('message', '')}",
    ]
    if request_id:
        details.append(f"request_id={request_id}")
    if reset_tokens:
        details.append(f"reset_tokens={reset_tokens}")
    if reset_requests:
        details.append(f"reset_requests={reset_requests}")
    return "; ".join(details)


def _retry_after_seconds(response: requests.Response) -> float | None:
    value = response.headers.get("retry-after")
    if value:
        try:
            return float(value)
        except ValueError:
            return None
    return None


def load_transcription(file_path: str) -> str:
    """Load transcription from file (supports .txt, .json)"""
    path = Path(file_path)

    if not path.exists():
        print(f"Error: Transcription file not found: {file_path}", file=sys.stderr)
        sys.exit(1)

    if path.suffix == ".json":
        data = json.loads(path.read_text())
        return data.get("text", "")
    else:
        return path.read_text()


def summarize_meeting(
    transcription: str,
    model: str,
    ollama_url: str,
) -> dict:
    """Generate meeting notes using the configured local or OpenAI text model."""
    provider = os.environ.get("SUMMARY_PROVIDER", "ollama").lower()
    print(f"Generating meeting summary using {provider}/{model}...", file=sys.stderr)
    prompt = SUMMARY_PROMPT.format(transcription=transcription)

    if provider == "openai":
        text = query_openai(prompt, model=model)
    elif provider == "ollama":
        text = query_ollama(prompt, model=model, ollama_url=ollama_url)
    else:
        raise RuntimeError(f"Unsupported SUMMARY_PROVIDER: {provider}")

    return {"summary": _strip_code_fence(text)}


def _strip_code_fence(text: str) -> str:
    """Strip wrapping code fences that models sometimes add around markdown output."""
    import re
    # Match optional language tag: ```markdown or ```md or just ```
    text = text.strip()
    text = re.sub(r"^```[a-z]*\n", "", text)
    text = re.sub(r"\n```$", "", text)
    return text.strip()


def save_summary(result: dict, output_file: str, format: str):
    """Save summary in specified format."""
    output_path = Path(output_file)

    if format in ("txt", "md"):
        output_path.write_text(result["summary"] + "\n")
    elif format == "json":
        output_path.write_text(json.dumps(result, indent=2))

    print(f"Summary saved to: {output_path}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="Summarize meeting transcription using Ollama")
    parser.add_argument("transcription_file", help="Path to transcription file (.txt or .json)")
    parser.add_argument("-m", "--model", default="llama3.1:8b",
                       help="Ollama model to use (default: llama3.1:8b)")
    parser.add_argument("-u", "--ollama-url", default=DEFAULT_OLLAMA_URL,
                       help=f"Ollama API URL (default: {DEFAULT_OLLAMA_URL})")
    parser.add_argument("-f", "--format", default="md",
                       choices=["txt", "md", "json"],
                       help="Output format (default: md)")
    parser.add_argument("-o", "--output",
                       help="Output file (default: transcription_file_summary.md)")


    args = parser.parse_args()

    # Load transcription
    transcription = load_transcription(args.transcription_file)

    if not transcription.strip():
        print("Error: Transcription is empty", file=sys.stderr)
        sys.exit(1)

    # Determine output file
    if args.output:
        output_file = args.output
    else:
        trans_path = Path(args.transcription_file)
        suffix = ".md" if args.format == "md" else f".{args.format}"
        output_file = trans_path.with_name(f"{trans_path.stem}_summary{suffix}")

    # Generate summary
    try:
        result = summarize_meeting(
            transcription,
            model=args.model,
            ollama_url=args.ollama_url,
        )

        # Save results
        save_summary(result, output_file, args.format)

        print("\nSummarization complete!", file=sys.stderr)

    except Exception as e:
        print(f"Error during summarization: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
