"""Domain rejection shared by lifecycle and transaction authority."""

class Rejected(Exception):
    def __init__(self, code, detail):
        self.code, self.detail = code, detail
        super().__init__(detail)
