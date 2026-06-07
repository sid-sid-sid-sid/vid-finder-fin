import cv2
import numpy as np
import torch
from pathlib import Path
from PIL import Image
from transformers import CLIPProcessor, CLIPModel

_GOOD_PROMPTS = [
    "a clear shot of a person's face",
    "a well-lit detailed scene",
    "a sharp focused image with visible objects",
    "a scene with recognizable content",
    "a movie or TV show scene",
]

_BAD_PROMPTS = [
    "a black or nearly black screen",
    "a blurry or out of focus image",
    "a solid color screen",
    "a transition frame or fade",
    "a very dark scene with no visible detail",
    "text on a plain background",
]

def load_clip():
    model_id = "openai/clip-vit-base-patch32"
    model = CLIPModel.from_pretrained(model_id)
    processor = CLIPProcessor.from_pretrained(model_id)
    model.eval()
    return model, processor

def sample_frames(video_path: str, n_samples: int = 32) -> list[tuple[int, np.ndarray]]:
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {video_path}")

    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if total <= 0:
        raise RuntimeError("Video has no readable frames.")

    indices = np.linspace(0, total - 1, min(n_samples, total), dtype=int)
    frames = []

    for idx in indices:
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(idx))
        ret, frame = cap.read()
        if ret:
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            frames.append((int(idx), rgb))

    cap.release()
    return frames

def encode_frames(
    frames: list[tuple[int, np.ndarray]],
    model,
    processor,
    batch_size: int = 64,
) -> np.ndarray:
    images = [Image.fromarray(f[1]) for f in frames]
    all_features = []

    for i in range(0, len(images), batch_size):
        batch = images[i : i + batch_size]
        inputs = processor(images=batch, return_tensors="pt", padding=True)

        with torch.no_grad():
            feats = model.get_image_features(**inputs)

        if not isinstance(feats, torch.Tensor):
            feats = getattr(feats, "image_embeds", getattr(feats, "pooler_output", feats))

        all_features.append(feats.numpy())

    embeddings = np.concatenate(all_features, axis=0)
    norms = np.linalg.norm(embeddings, axis=1, keepdims=True) + 1e-8
    return embeddings / norms

def score_clarity(
    frames: list[tuple[int, np.ndarray]],
    embeddings: np.ndarray,
    model,
    processor,
) -> np.ndarray:
    all_prompts = _GOOD_PROMPTS + _BAD_PROMPTS
    text_inputs = processor(
        text=all_prompts, return_tensors="pt", padding=True, truncation=True
    )

    with torch.no_grad():
        text_feats = model.get_text_features(**text_inputs)
        if not isinstance(text_feats, torch.Tensor):
            text_feats = getattr(text_feats, "text_embeds",
                        getattr(text_feats, "pooler_output", text_feats))
        text_feats = text_feats.numpy()

    text_feats /= np.linalg.norm(text_feats, axis=1, keepdims=True) + 1e-8

    sims = embeddings @ text_feats.T

    n_good = len(_GOOD_PROMPTS)
    good_scores = sims[:, :n_good].mean(axis=1)
    bad_scores  = sims[:, n_good:].mean(axis=1)

    raw = good_scores - bad_scores
    low, high = raw.min(), raw.max()
    if high - low < 1e-6:
        return np.ones(len(frames))
    return (raw - low) / (high - low)

def select_best_frames(
    frames: list[tuple[int, np.ndarray]],
    embeddings: np.ndarray,
    clarity_scores: np.ndarray,
    n_select: int = 8,
    clarity_weight: float = 0.5,
) -> list[tuple[int, np.ndarray, np.ndarray]]:
    n = len(frames)
    if n <= n_select:
        return [(frames[i][0], frames[i][1], embeddings[i]) for i in range(n)]

    threshold = np.percentile(clarity_scores, 25)
    eligible = np.where(clarity_scores >= threshold)[0]

    if len(eligible) <= n_select:
        indices = sorted(eligible.tolist())
        return [(frames[i][0], frames[i][1], embeddings[i]) for i in indices]

    sub_emb    = embeddings[eligible]
    sub_clar   = clarity_scores[eligible]

    seed = int(np.argmax(sub_clar))
    selected = [seed]
    min_dists = 1 - (sub_emb @ sub_emb[seed])

    for _ in range(n_select - 1):
        combined = (1 - clarity_weight) * min_dists + clarity_weight * sub_clar
        combined[selected] = -1  
        next_idx = int(np.argmax(combined))
        selected.append(next_idx)
        new_dists = 1 - (sub_emb @ sub_emb[next_idx])
        min_dists = np.minimum(min_dists, new_dists)

    orig_indices = sorted([eligible[i] for i in selected])

    return [(frames[i][0], frames[i][1], embeddings[i]) for i in orig_indices]

def extract_distinctive_frames(
    video_path: str,
    model,
    processor,
    n_sample: int = 32,
    n_select: int = 8,
) -> list[tuple[int, np.ndarray, np.ndarray]]:
    frames = sample_frames(video_path, n_samples=n_sample)
    embeddings = encode_frames(frames, model, processor)
    clarity = score_clarity(frames, embeddings, model, processor)
    return select_best_frames(frames, embeddings, clarity, n_select=n_select)

def save_frames(
    distinctive: list[tuple[int, np.ndarray, np.ndarray]],
    out_dir: str,
) -> list[str]:
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    saved = []

    for frame_idx, rgb, _ in distinctive:
        fpath = str(out_path / f"frame_{frame_idx:06d}.jpg")
        img = Image.fromarray(rgb)
        img.save(fpath, "JPEG", quality=92)
        saved.append(fpath)

    return saved