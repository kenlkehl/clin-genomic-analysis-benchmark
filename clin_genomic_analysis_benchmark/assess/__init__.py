"""The assess task: one prompt per question, one JSON verdict per component.

The prompt is the assess rulebook and the shared conventions, followed by the
question. The model gets no tools and no data; ``backends`` sends the prompt
to a model API and ``runner`` records and validates the replies.
"""
