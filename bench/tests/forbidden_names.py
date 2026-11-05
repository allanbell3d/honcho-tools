"""SHA-256 hashes of lower-case words that must never appear anywhere under bench/.

They are the names of people, peers, workspaces and hosts in the private data the numbers came from. Only hashes are
kept here so this file does not carry them. Add a hash (python -c "import hashlib; print(hashlib.sha256(b'word').hexdigest())")
whenever a new name turns up in your own data.
"""
FORBIDDEN_NAME_HASHES = {
    "5fd726bc56c3e86906cba8814a9b97d97f2ab209c48d2441ca5ac9e52b09e630",
    "f683740cded6680d9d94dfe6a258e44c96a27278a89e15acb52c0b8f32453d38",
    "e927917321ac66d61c0e51a75c39a0d77a6509b6af22a7783961e4ead703650c",
    "d91f6de6c22475779b288d6300d65144743c3d06928a3a3441a103765b9ee7e6",
    "823b5ef47cdb3cc91e9788d700470e763889184ea9ddc082b12769f7f3fb8aec",
    "ce29c26322abc2113a235381220cb924bc6094a013b8633e4fefa648d53ab649",
    "b30dfe3a359c4dfbd77bc1baf9b28c6e7ba822da313b96e6593f52ea5184c001",
    "decaf8a68769104d4c94458884eb5573fec905aea671af6d69e40dde7c9b14e3",
    "65ebae8e077a037d81231c1bde91b721bf23bc3dd92baebdcdbe295ade66b79f",
    "02b15ef87562676a8a42f33af9a0ca6aa11e491abda98cec24e4eded61ffa78d",
    "bd3dae5fb91f88a4f0978222dfd58f59a124257cb081486387cbae9df11fb879",
    "693b286515bd1dd00865e7b60e4e53556537bbe4b1cc90ab608d94eb7c56fdc6",
    "047de0f543c7e3596bb6dbd0e65f2c30357720a63eca423f08c52fae314f6806",
    "22ff277b10da3a6fbabf7b4a7385855f24fe3bb04a4688bbf35d092a0740344c",
}
