# forms/fields.py
from wtforms import IntegerField
from wtforms.validators import NumberRange

class NumberInputField(IntegerField):
    """An IntegerField rendered as <input type="number">."""
    def __init__(self, *args, **kwargs):
        render_kw = kwargs.setdefault("render_kw", {})
        render_kw.setdefault("type", "number")
        super().__init__(*args, **kwargs)
