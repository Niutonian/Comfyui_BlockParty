# Creating a 45-second news video with Block Party

This records the method used for the Nepal flood-recovery video. There are two ComfyUI jobs: image generation and image-to-video generation. A separate FFmpeg assembly step produces the final film.

## 1. Select and verify the news

Browse recent reporting and choose a story with a reliable primary source. Check the publication date, what happened, and the facts that will appear in narration or captions. Save the source URL and date with the job.

For this film, the source was UNICEF's **9 October 2026** briefing:
https://www.unicef.org/press-releases/nepal-long-road-recovery

Use short original summaries. Illustrations should support verified facts rather than invent additional events. The people and scenes in this film are fictional composites; an on-screen label identifies them as AI-generated illustrations.

## 2. Write five scenes

Divide the story into **five nine-second scenes**, giving a total of 45 seconds. Each scene needs three pieces of text:

- A visual description for its reference image.
- A short narration line that fits comfortably within nine seconds.
- A concise caption containing the key fact.

The Nepal story follows this sequence:

| Scene | Image | Story point |
| --- | --- | --- |
| 1 | Himalayan river valley and damaged bridge | Nepal faces a long recovery |
| 2 | A family beside a flood-damaged village | More than 32,000 children affected |
| 3 | An empty damaged classroom | Damage to schools and water systems |
| 4 | Orderly blanket distribution | Families need support as winter approaches |
| 5 | Children learning in a temporary classroom | Recovery includes education and care |

The exact visual descriptions, narration and captions are in `SCENES` in `security_review/serious_news_20261011.py`.

## 3. Generate the reference images

Run the image job sequentially, one scene at a time:

`jobs/serious_news_qwen_20261011/`

| Setting | Value used |
| --- | --- |
| Image model | `qwen_image_2.1_int8_convrot.safetensors` |
| Text encoder | `qwen3vl_8b_int8_convrot.safetensors`, on CPU |
| VAE | `qwen_image_2.1_vae_bf16.safetensors` |
| Output size | 768 × 432 |
| Batch size | 1 |
| Sampling | 25 steps, CFG 1.0, Euler, simple scheduler |
| Qwen cache | CPU, int8 |
| VAE decode | Tiled, tile size 256, overlap 64 |

Write one prompt per file, such as `input/scene_01.txt`. Request consistent restrained photorealistic illustrations, natural anatomy, horizontal framing, and no embedded text or logos. Inspect the generated images before using them as references.

After image generation finishes and the queue is idle, unload the image models and free memory before starting the video job.

## 4. Render, upscale and refine each video scene

Use the generated image as the single reference input for:

`jobs/serious_news_upscaled_20261011/`

The video graph was adapted from `jobs/test2_mcp/workflow.api.json`. Qwen generates the still images; the MiniMax H3 workflow generates the video and audio.

For each scene, pair `scene_01.png` with `scene_01.txt`. The text asks the video model to preserve the reference, use subtle motion and slow camera drift, maintain stable people and architecture, and generate a calm English voice with the specified narration. It also requests quiet natural ambience, no music, no other speech, and a quiet ending.

Each clip uses this sequence:

1. **First pass:** request nine seconds, use the 0.35-megapixel resolution setting, and run the first eight steps of a twelve-step schedule. The resulting base video is 800 × 448.
2. **Latent upscale:** apply `MinimaxH3LatentUpscaler3D` using `minimax_h3_latent_upscaler_3d_conv_v1_fp16.safetensors`, with scale 1.5, temporal chunking enabled, and force unload enabled.
3. **Refinement:** feed the upscaled video latent, together with the audio latent, into the remaining four sampling steps. Decode and save the refined result.

The actual refined size is **1216 × 672**, due to the workflow's dimension alignment. Each raw saved clip is approximately **9.4167 seconds**, under the ten-second limit. Assembly trims each clip to nine seconds.

Both stages are retained: `output/scene_01.mp4` is the refined output; `output/scene_01_2.mp4` is the base output. The final assembly uses the refined files.

## 5. Control memory and monitor progress

The render used an RTX 5080 with approximately 16 GB VRAM. Run scenes sequentially, use batch size one, keep clips below ten seconds, enable temporal chunking, and unload models between the image and video jobs.

ComfyUI was running with:

```text
--use-sage-attention --disable-comfy-compiler --reserve-vram 2
```

The compiler was disabled after a separate compiler error during the earlier happy-news production. That error was not an OOM. These settings worked on this installation; they do not guarantee the same memory use on another machine.

Block Party checks the dependency contract before rendering and records item status, queue progress, and the exact submitted workflow. The automation includes lower-resolution retries for OOM, while retaining the upscaling stages. No OOM retry was needed for this serious-news film.

Editable prompt files move to `state/claimed/` when claimed for a run. The exact submitted API graphs are in `state/workflows/`; `state/files/` contains status records, not ComfyUI workflows.

## 6. Assemble the final film

Use FFmpeg to process each refined clip:

- Trim to nine seconds and pad audio if necessary.
- Scale while preserving aspect ratio, then crop to 1280 × 720.
- Set square pixels and 24 fps.
- Add the scene caption and the footer `UNICEF | 9 Oct 2026 | AI-generated illustration`.
- Encode H.264 video and AAC audio.

Concatenate the five edited scenes. Normalize timestamps, pad the ending if needed, and trim both video and audio to exactly 45 seconds. Encode the final video at 24 fps, 1080 frames, H.264 CRF 18, with AAC audio and MP4 fast-start enabled.

Final file:

`jobs/serious_news_upscaled_20261011/output/nepal_recovery_45_seconds.mp4`

## 7. Validate the result

Check all five scenes have both saved stages, that the refined dimensions exceed the base dimensions, and that the saved graph actually connects the upscaler to the refinement sampler. Confirm the submitted prompts match their claimed text files.

Use FFprobe to confirm **1280 × 720, 1080 video frames, and exactly 45 seconds for both video and audio**. Decode the entire final file with FFmpeg to detect media errors. Extract frames to inspect caption placement and image quality.

These checks passed. Generated narration was not independently transcribed; technical validation alone does not establish that every spoken word matches the script.

## Scripts and rerunning

Run commands from the Block Party project folder. The production script is specific to this story, uses the existing linked ComfyUI installation and job manifests, and reuses helper functions from `security_review/happy_news_20261011.py`. It is not a standalone portable news generator. Existing completed clips are reused.

```powershell
# Generate missing images and clips, then assemble.
python -u -m security_review.serious_news_20261011

# Rebuild the edit from the existing refined clips without GPU rendering.
python -u -m security_review.serious_news_20261011 --assemble-only

# Validate final timing and submitted prompts, and extract preview frames.
python -m security_review.serious_news_verify
```

For a new story, create new job names and folders, update the source, scene descriptions, narration, captions and footer, and review the dependency manifests. Do not reuse existing output names unintentionally: the production script skips clips that already exist.

To share the generation workflows, export both jobs as Block Party ZIPs. Reproducing the whole film also requires the assembly script and its helper module, or equivalent editing instructions. Each export packages one job; it does not automatically chain image generation, video generation and assembly.
