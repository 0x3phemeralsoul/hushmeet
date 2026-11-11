#!/bin/bash

# Audio recording script for meetings
# Records system audio and microphone to a WAV file

set -euo pipefail

# Check for dependencies
if ! command -v pactl &> /dev/null; then
    echo "Error: pactl not found. Install pulseaudio-utils or pipewire-pulse"
    exit 1
fi

if ! command -v ffmpeg &> /dev/null; then
    echo "Error: ffmpeg not found. Install ffmpeg"
    exit 1
fi

# Configuration
OUTPUT_DIR="${RECORDINGS_DIR:-./recordings}"
DATE=$(date +%Y%m%d)
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
RECORDING_STARTED_EPOCH=$(date +%s)
RECORDING_LABEL=$(date +%Y-%m-%d:%H)
OUTPUT_FILE=""
AUTO_NAMED=false

# Parse arguments
DURATION=""
TITLE=""
while [[ $# -gt 0 ]]; do
    case $1 in
        -d|--duration)
            DURATION="$2"
            shift 2
            ;;
        -o|--output)
            OUTPUT_FILE="$2"
            shift 2
            ;;
        -t|--title)
            TITLE="$2"
            shift 2
            ;;
        -h|--help)
            echo "Usage: $0 [-d DURATION] [-o OUTPUT_FILE] [-t TITLE]"
            echo "  -d, --duration    Recording duration (e.g., 3600 for 1 hour)"
            echo "  -o, --output      Output file path (default: timestamp + title in RECORDINGS_DIR)"
            echo "  -t, --title       Meeting title (included in the default folder and audio filename)"
            echo ""
            echo "Environment variables:"
            echo "  RECORDINGS_DIR    Directory for recordings (default: ./recordings)"
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

# The duration is unknown until the recording ends, so automatically named
# recordings start in a private temporary directory and are atomically renamed
# to their final user-facing name afterwards. Custom --output paths are left
# untouched.
if [ -z "$OUTPUT_FILE" ]; then
    AUTO_NAMED=true
    TEMP_BASENAME=".recording-${TIMESTAMP}"
    MEETING_DIR="${OUTPUT_DIR}/${DATE}/${TEMP_BASENAME}"
    OUTPUT_FILE="${MEETING_DIR}/${TEMP_BASENAME}.wav"
fi

# Create output directory
mkdir -p "$(dirname "$OUTPUT_FILE")"

# Determine audio source.
# Every HushMeet recording mixes participant/system sound with a microphone.
# AUDIO_MICROPHONE_SOURCE optionally selects that microphone. AUDIO_SOURCE is
# accepted as a legacy alias only when it names a non-monitor source.
# AUDIO_SOURCE_TYPE controls what to capture:
#   "microphone" (default) - default PulseAudio/PipeWire source (mic)
#   "monitor"              - monitor of default sink (captures output audio,
#                            needed for meeting audio on BT headsets)
#   "both"                 - mix microphone + sink monitor into one recording
#                            (captures both sides of a call)
if [ -z "${AUDIO_MICROPHONE_SOURCE:-}" ] \
    && [ -n "${AUDIO_SOURCE:-}" ] \
    && [[ "$AUDIO_SOURCE" != *.monitor ]]; then
    AUDIO_MICROPHONE_SOURCE="$AUDIO_SOURCE"
fi
AUDIO_SOURCE_TYPE="both"
RECORD_SOURCE=""  # mixed below

echo "Recording audio..." >&2
echo "Output file: $OUTPUT_FILE" >&2
if [ -n "$RECORD_SOURCE" ]; then
    echo "Audio source: $RECORD_SOURCE" >&2
else
    echo "Audio source: mic + output mix" >&2
fi
if [ -n "$DURATION" ]; then
    echo "Duration: ${DURATION}s" >&2
fi
echo "" >&2
echo "Press Ctrl+C to stop recording" >&2

# RECORD_BACKEND controls the recording tool:
#   "ffmpeg" (default) - ffmpeg with PulseAudio compat layer
#   "pw-record"        - pw-record, talks directly to PipeWire graph;
#                        required for BT headsets in HSP/HFP mode where
#                        ffmpeg -f pulse captures silence
RECORD_BACKEND="${RECORD_BACKEND:-ffmpeg}"

set +e
ffmpeg_exit_code=0

if [ "$AUDIO_SOURCE_TYPE" = "both" ]; then
    # Mix the chosen microphone and sink monitor into one recording.
    MIC_SOURCE="${AUDIO_MICROPHONE_SOURCE:-$(pactl get-default-source)}"
    MONITOR_SOURCE="$(pactl get-default-sink).monitor"
    echo "Mixing mic ($MIC_SOURCE) + monitor ($MONITOR_SOURCE)" >&2

    FFMPEG_ARGS=(
        -f pulse -i "$MIC_SOURCE"
        -f pulse -i "$MONITOR_SOURCE"
        -filter_complex amix=inputs=2:duration=longest
        -ar 16000 -ac 1 -c:a pcm_s16le
    )
    [ -n "$DURATION" ] && FFMPEG_ARGS+=(-t "$DURATION")
    FFMPEG_ARGS+=("$OUTPUT_FILE")

    ffmpeg "${FFMPEG_ARGS[@]}" >&2
    ffmpeg_exit_code=$?

elif [ "$RECORD_BACKEND" = "pw-record" ]; then
    PW_ARGS=(--target "$RECORD_SOURCE" --rate 16000 --channels 1 --format s16)
    if [ -n "$DURATION" ]; then
        timeout "$DURATION" pw-record "${PW_ARGS[@]}" "$OUTPUT_FILE" >&2
    else
        pw-record "${PW_ARGS[@]}" "$OUTPUT_FILE" >&2
    fi
    ffmpeg_exit_code=$?

else
    FFMPEG_ARGS=(-f pulse -i "$RECORD_SOURCE")
    [ -n "$DURATION" ] && FFMPEG_ARGS+=(-t "$DURATION")
    FFMPEG_ARGS+=(-ar 16000 -ac 1 -c:a pcm_s16le "$OUTPUT_FILE")

    ffmpeg "${FFMPEG_ARGS[@]}" >&2
    ffmpeg_exit_code=$?
fi

set -e

recording_duration_label() {
    local audio_file="$1"
    local elapsed_seconds="$2"
    local duration_seconds=""
    if command -v ffprobe &> /dev/null; then
        duration_seconds=$(ffprobe -v error -show_entries format=duration \
            -of default=noprint_wrappers=1:nokey=1 "$audio_file" 2>/dev/null || true)
        duration_seconds="${duration_seconds%%.*}"
    fi
    if ! [[ "$duration_seconds" =~ ^[0-9]+$ ]]; then
        duration_seconds="$elapsed_seconds"
    fi
    [ "$duration_seconds" -lt 0 ] && duration_seconds=0
    printf '%02d:%02d' "$((duration_seconds / 3600))" "$(((duration_seconds % 3600) / 60))"
}

finalize_automatic_name() {
    local duration_label="$1"
    local title_component
    title_component=$(printf '%s' "$TITLE" \
        | tr '\n\r' '  ' \
        | sed -E 's/[[:space:]]+/ /g; s/^[[:space:]]+//; s/[[:space:]]+$//; s#[/\\:*?"<>|]#-#g' \
        | cut -c1-100)
    local final_basename="${RECORDING_LABEL} - ${duration_label}"
    if [ -n "$title_component" ]; then
        final_basename="${final_basename} — ${title_component}"
    else
        final_basename="${final_basename} — Meeting"
    fi
    local final_dir="${OUTPUT_DIR}/${DATE}/${final_basename}"
    local suffix=2
    while [ -e "$final_dir" ]; do
        final_dir="${OUTPUT_DIR}/${DATE}/${final_basename} (${suffix})"
        suffix=$((suffix + 1))
    done
    mv "$OUTPUT_FILE" "${MEETING_DIR}/${final_basename}.wav"
    mv "$MEETING_DIR" "$final_dir"
    OUTPUT_FILE="${final_dir}/${final_basename}.wav"
}

# Always output filename if file was created, even if interrupted
if [ -f "$OUTPUT_FILE" ]; then
    RECORDED_DURATION_LABEL=$(recording_duration_label "$OUTPUT_FILE" "$(( $(date +%s) - RECORDING_STARTED_EPOCH ))")

    echo "" >&2
    echo "Recording saved to: $OUTPUT_FILE" >&2

    # Prompt for title if not provided and a terminal is available
    if [ -z "$TITLE" ]; then
        if read -p "Meeting title (optional): " TITLE </dev/tty 2>/dev/null; then
            : # title captured
        else
            TITLE=""
        fi
    fi

    if [ "$AUTO_NAMED" = true ]; then
        finalize_automatic_name "$RECORDED_DURATION_LABEL"
        echo "Final recording name: $(basename "$OUTPUT_FILE")" >&2
    fi

    # Create metadata file
    meeting_dir=$(dirname "$OUTPUT_FILE")
    base_name=$(basename "$OUTPUT_FILE" .wav)
    metadata_file="${meeting_dir}/${base_name}_metadata.json"

    # Generate metadata JSON
    cat > "$metadata_file" << EOF
{
  "title": "${TITLE}",
  "timestamp": "${TIMESTAMP}",
  "duration": "${RECORDED_DURATION_LABEL}",
  "date": "${DATE}",
  "audio_file": "$(basename "$OUTPUT_FILE")",
  "created_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "source": "local_recording"
}
EOF

    if [ -n "$TITLE" ]; then
        echo "Title: $TITLE" >&2
    fi

    # Output only the filename to stdout (for script capture)
    echo "$OUTPUT_FILE"
else
    echo "" >&2
    echo "Error: Recording file was not created" >&2
    exit 1
fi
