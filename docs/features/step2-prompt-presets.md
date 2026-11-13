# Step 2 prompt presets

Open Setup → Advanced → Prompt studio → Cast, attribution and segmentation prompts. The Step 2: speaker attribution editor contains its preset selector, description, prompt shape, Save preset and Setup, Delete preset and save Setup, and What the model will see controls beside the system prompt and user instruction.

Choose a built-in preset to load its prompt shape and text. Edit the fields, then use Save preset and Setup to name your own copy. Saving Setup with an edited built-in also creates and selects a named edited copy. Both actions save other edited Setup settings. Custom presets retain their original prompt shape, including the worked example for michel2_shot. Default-shaped user templates must retain {roster} and {batch}.

Delete removes a custom preset after confirmation and selects michel2_full. Built-ins remain protected. Switching presets with unsaved edits requires confirmation; cancelled or failed saves retain the editor contents. Use What the model will see to inspect the composed request. These controls use the existing attribution preset configuration for ordinary Pass 2, single-pass generation, batch generation, and manual transport.
