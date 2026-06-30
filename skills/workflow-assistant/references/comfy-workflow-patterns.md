# Comfy Workflow Patterns

Use these patterns as defaults when creating or repairing ComfyUI workflows. Adapt to the installed node types and the model family already in the graph.

## Common Slot Types

- `MODEL` -> sampler `model`
- `CLIP` -> text encode `clip`
- `CONDITIONING` -> sampler `positive` or `negative`
- `LATENT` -> sampler `latent_image` or decoder `samples`
- `VAE` -> encoder/decoder `vae`
- `IMAGE` -> image processors, previews, or save nodes
- `MASK` -> mask processors, inpaint conditioning, or composite nodes

## Basic Text-to-Image

```mermaid
flowchart LR
  Model[Model Loader] --> Sampler[KSampler]
  CLIP[CLIP Loader] --> Pos[Positive Prompt]
  CLIP --> Neg[Negative Prompt]
  Pos --> Sampler
  Neg --> Sampler
  Latent[Empty Latent] --> Sampler
  Sampler --> Decode[VAE Decode]
  VAE[VAE Loader] --> Decode
  Decode --> Save[Save Image]
```

Defaults:

- Use separate positive and negative prompt nodes unless the user requests identical conditioning.
- Keep one shared loader set for one model family.
- Typical KSampler ranges depend on model family; inspect the current workflow before changing them.

## Image-to-Image

Replace `EmptyLatentImage` with:

```mermaid
flowchart LR
  Input[Load Image] --> Encode[VAE Encode]
  VAE[VAE Loader] --> Encode
  Encode --> Sampler[KSampler]
```

Use denoise below `1.0` when preserving source composition. Higher denoise changes more of the image.

## Upscale or Refine

```mermaid
flowchart LR
  BaseImage[Base Image] --> Upscale[Upscale Image]
  Upscale --> Encode[VAE Encode]
  Encode --> RefinerSampler[KSampler]
  RefinerSampler --> Decode[VAE Decode]
  Decode --> Save[Save Refined]
```

Keep the first pass and refine pass visually grouped. Name save prefixes so outputs can be compared.

## LoRA Insertion

Typical placement:

```mermaid
flowchart LR
  ModelLoader --> LoRA[LoRA Loader]
  CLIPLoader --> LoRA
  LoRA --> Sampler
  LoRA --> PromptEncode
```

Do not add a LoRA blindly. Confirm model family compatibility and the target loader path first.

## Multi-Prompt Branching

Use shared model, CLIP, VAE, and latent settings when comparing prompt variations:

```mermaid
flowchart LR
  Model --> K1[KSampler 1]
  Model --> K2[KSampler 2]
  CLIP --> P1[Prompt 1]
  CLIP --> P2[Prompt 2]
  Neg[Shared Negative] --> K1
  Neg --> K2
  Latent --> K1
  Latent --> K2
  K1 --> D1[Decode 1] --> S1[Save 1]
  K2 --> D2[Decode 2] --> S2[Save 2]
```

Use fixed seeds for controlled prompt comparisons and random seeds for exploratory batches.

## Debugging Probe Pattern

Use temporary preview/probe nodes at module boundaries:

- After image load or preprocessing
- After mask creation
- After VAE decode
- After upscale or composite
- Before final save

Debug one hypothesis at a time. Remove or bypass probes when the workflow is stable.

## Layout Guidance

- Keep dataflow left-to-right unless the current graph clearly uses another convention.
- Stack shared loaders vertically on the left.
- Place prompt/conditioning near the center.
- Place samplers after conditioning.
- Place decode/save/output nodes on the right.
- Group repeated branches and name groups by purpose.
- After layout changes, fit the view and take a screenshot.
