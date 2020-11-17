# credits to @lucidrains https://github.com/lucidrains

import torch
from torch import nn, einsum
import torch.nn.functional as F
from functools import partial
from inspect import isfunction

from einops import rearrange, repeat

# helpers

def exists(val):
    return val is not None

def default(val, d):
    if exists(val):
        return val
    return d() if isfunction(d) else d

"""## Helpers and FeedForward"""

# helper classes 
# based on https://github.com/lucidrains/all-normalization-transformer/blob/master/all_normalization_transformer/all_normalization_transformer.py

class Residual(nn.Module):
    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def forward(self, x, *args, **kwargs):
        return self.fn(x, *args, **kwargs) + x
# Added *args, **kwargs here to pass context and masks
class PostNorm(nn.Module):
    def __init__(self, dim, fn):
        super().__init__()
        self.fn = fn
        self.norm = nn.LayerNorm(dim)

    def forward(self, x, *args, **kwargs):
        x = self.fn(x)
        return self.norm(x, *args, **kwargs)

class PreNorm(nn.Module):
    def __init__(self, dim, fn):
        super().__init__()
        self.fn = fn
        self.norm = nn.LayerNorm(dim)

    def forward(self, x, *args, **kwargs):
        x = self.norm(x)
        return self.fn(x, *args, **kwargs)

class FeedForward(nn.Module):
    def __init__(self, dim, mult = 4, dropout=0.):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(dim, dim * 4),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(dim * 4, dim),
            nn.Dropout(dropout)
        )
    def forward(self, x):
        return self.net(x)


"""## Attention"""

MASK_VAL = -1e4 # instead of float('-inf') to make fp16 work

class Attention(nn.Module):
    def __init__(self, 
                 dim, 
                 heads = 8, 
                 causal = False,
                 mask = None,
                 dropout=0.1,
                 store_attention=False):
        super().__init__()
        self.causal = causal
        self.store_attention = store_attention
        self.mask = mask #??
        self.heads = heads
        self.scale = dim ** -0.5
        
        self.to_q = nn.Linear(dim, dim, bias = False)
        self.to_kv = nn.Linear(dim, dim * 2, bias = False)
        self.dropout = nn.Dropout(dropout)

        self.to_out = nn.Linear(dim, dim)

    def forward(self, x, context = None, mask = None, context_mask = None):
        b, n, _, h, device = *x.shape, self.heads, x.device
        kv_input = default(context, x)

        q = self.to_q(x) # replaced q_ with q (don't need to store it fore basic tfmr)
        kv = self.to_kv(kv_input).chunk(2, dim = -1)

        q, k, v = map(lambda t: rearrange(t, 'b n (h d) -> b h n d', h = h), (q, *kv))
        # boolean input_mask is False at positions not to attend to
        input_mask = None
        if any(map(exists, (mask, context_mask))):
            q_mask = default(mask, lambda: torch.ones((b, n), device = device).bool())
            k_mask = q_mask if not exists(context) else context_mask
            k_mask = default(k_mask, lambda: torch.ones((b, k.shape[-2]), device = device).bool())
            q_mask = rearrange(q_mask, 'b i -> b () i ()')
            k_mask = rearrange(k_mask, 'b j -> b () () j')
            input_mask = q_mask * k_mask
        # classic dot-product attention
        dots = torch.einsum('bhid,bhjd->bhij', q, k) * self.scale
        # might need to tune MASK_VAL for fp16 to work
        if exists(input_mask):
            dots.masked_fill_(~input_mask, MASK_VAL)
            del input_mask

        if self.causal:
            i, j = dots.shape[-2:]
            mask = torch.ones((i, j), device = device).triu_(j - i + 1).bool()
            dots.masked_fill_(mask, MASK_VAL)
            del mask

        attn = F.softmax(dots, -1)
        attn_ = self.dropout(attn) #? to return attention before dropout

        out = torch.einsum('bhij,bhjd->bhid', attn_, v)
        out = rearrange(out, 'b h n d -> b n (h d)')
        out =  self.to_out(out)
        #out = self.dropout(out) # option for more dropout here
        #TODO store or return atteention matrix
        # if self.store_attention:
        #     return out, attn
        return out


"""## Transformer blocks

### Encoder
"""

class TransformerEncoderBlock(nn.Module):
    """
    Bacis transformer encoder block. Consists of multi-head attention and positional feedforward layers
    """
    def __init__(self, dim, heads = 8, causal = False, mask = None, 
                 attn_dropout=0.1, ff_dropout=0.1):
        super().__init__()
        self.attn = Residual(PreNorm(dim, Attention(dim, causal=causal, dropout=attn_dropout)))
        self.ff = Residual(PreNorm(dim, FeedForward(dim, dropout=ff_dropout)))
    def forward(self, x, mask=None): #? more args
        out = self.attn(x, mask=mask)
        out = self.ff(out)
        return out

class TransformerEncoder(nn.Module):
    def __init__(self, dim, depth=6, heads=8, causal=False, attn_dropout=0.1, ff_dropout=0.1):
        super().__init__()
        self.dim = dim
        self.layers = nn.ModuleList([])
        for _ in range(depth):
            self.layers.append(TransformerEncoderBlock(dim, heads, causal=causal, attn_dropout=attn_dropout, ff_dropout=ff_dropout))
    def forward(self, x, mask=None):
        for layer in self.layers:
            x = layer(x, mask=mask)
        return x

"""Decoder block has attention and cross attention

### Decoder
"""

class TransformerDecoderBlock(nn.Module):
    def __init__(self, dim, heads = 8, mask = None, 
                 attn_dropout=0.1, ff_dropout=0.1):
        super().__init__()
        self.attn = Residual(PreNorm(dim, Attention(dim, causal=True, dropout=attn_dropout)))
        self.cross = Residual(PreNorm(dim, Attention(dim, causal=False, dropout=attn_dropout)))
        self.ff = Residual(PreNorm(dim, FeedForward(dim, dropout=ff_dropout)))

    def forward(self, x, context, mask=None, context_mask=None):
        out = self.attn(x, mask=mask)
        out = self.cross(out, context, mask=mask, context_mask=context_mask)
        out = self.ff(out)
        return out

class TransformerDecoder(nn.Module):
    def __init__(self, dim, depth=6, heads=8, attn_dropout=0.1, ff_dropout=0.1):
        super().__init__()
        self.dim = dim
        self.layers = nn.ModuleList([])
        for _ in range(depth):
            self.layers.append(TransformerDecoderBlock(dim, heads, attn_dropout=attn_dropout, ff_dropout=ff_dropout))
    def forward(self, x, context, mask=None, context_mask=None):
        for layer in self.layers:
            x = layer(x, context, mask, context_mask)
        return x

"""### Models"""
# from https://github.com/lucidrains/reformer-pytorch/blob/master/reformer_pytorch/reformer_pytorch.py#L609

class AbsolutePositionalEmbedding(nn.Module):
    def __init__(self, dim, max_seq_len):
        super().__init__()
        self.emb = nn.Embedding(max_seq_len, dim)

    def forward(self, x):
        t = torch.arange(x.shape[1], device=x.device)
        return self.emb(t)

class FixedPositionalEmbedding(nn.Module):
    def __init__(self, dim):
        super().__init__()
        inv_freq = 1. / (10000 ** (torch.arange(0, dim, 2).float() / dim))
        self.register_buffer('inv_freq', inv_freq)

    def forward(self, x):
        t = torch.arange(x.shape[1], device=x.device).type_as(self.inv_freq)
        sinusoid_inp = torch.einsum("i,j->ij", t, self.inv_freq)
        emb = torch.cat((sinusoid_inp.sin(), sinusoid_inp.cos()), dim=-1)
        return emb[None, :, :]
#TODO add axial positional encodings

class TransformerEmbedding(nn.Module):
    """
    Combines token embedings with positional encodings
    pos_enc: str from {'absolute', 'fixed', 'axial'}
    """
    def __init__(self, emb_sz, dim, max_seq_len=512, dropout=0., pos_enc='absolute'):
        super().__init__()
        self.emb = nn.Embedding(emb_sz, dim)
        if pos_enc == 'absolute':
            self.pos_enc = AbsolutePositionalEmbedding(dim, max_seq_len)
        elif pos_enc == 'fixed':
            self.FixedPositionalEmbedding(dim)
        elif pos_enc == 'axial':
            raise NotImplementedError
        self.dropout = nn.Dropout(dropout)
        self._init()
    def forward(self, x):
        _, n = x.shape
        x = self.emb(x)
        x += self.pos_enc(torch.arange(n, device=x.device))
        return self.dropout(x)
    def _init(self):
        nn.init.normal_(self.emb.weight, std = 0.02)
        if getattr(self.pos_enc, 'weight', None):
            nn.init.normal_(self.pos_enc.weight, std = 0.02)

#TODO test weight tying
# Note on weight tying: it's done like here in fastai AWD_LSTM model
# Lucidrains does it with custom MatrixMultiply module https://github.com/lucidrains/reformer-pytorch/blob/master/reformer_pytorch/reformer_pytorch.py#L106
class TransformerEncDec(nn.Module):
    """
    Basic Transformer Encoder-Decoder model
    Parameters:
        * enc_vocab_sz: int - source vocab size 
        * dec_vocab_sz: int - target vocab size
        * dim: int - inner dimension of the model
        * depth: int (default: 6) 
        * heads: int (default: 8)
        * max_seq_len: int (default: 512)
        * pad_idx: int - padding token id, if pad_idx is provided, and no mask/context_mask are passed to 
                forward method will be used to generate padding masks
        * tie_weights: bool - if True target embedding weights are used for computation output projection
    Inputs:
        * src - source input ids, shape [bs, src_sl]
        * tgt - target input ids, shape [bs, tgt_sl]
        * src_mask - optional boolean source mask, shape [bs, src_sl]
        * tgt_mask - optional boolean target mask, shape [bs, tgt_sl]
    Returns:
        * logits - target token logits, shape [bs, tgt_sl, tgt_vocab_sz]
    """
    def __init__(self, enc_vocab_sz, dec_vocab_sz, dim, depth=6, heads=8, 
                 max_seq_len=512, pad_idx=None, tie_weights=False):
        super().__init__()
        self.max_seq_len = max_seq_len
        self.pad_idx = pad_idx
        self.enc_emb = TransformerEmbedding(enc_vocab_sz, dim, max_seq_len)
        self.dec_emb = TransformerEmbedding(dec_vocab_sz, dim, max_seq_len)
        self.encoder = TransformerEncoder(dim, depth, heads)
        self.decoder = TransformerDecoder(dim, depth, heads)
        self.proj = nn.Linear(dim, dec_vocab_sz)
        if tie_weights: self.proj.weight = self.emb.emb.weight

    def forward(self, src, tgt, src_mask = None, tgt_mask = None):
        src_mask = default(src_mask, self.get_padding_mask(src))
        tgt_mask = default(tgt_mask, self.get_padding_mask(tgt))
        enc = self.encoder(self.enc_emb(src), mask = src_mask)
        out = self.decoder(self.dec_emb(tgt), context=enc, mask=tgt_mask, context_mask=src_mask)
        return self.proj(out)
    def get_padding_mask(self, x):
        if self.pad_idx is None: return None
        return (x != self.pad_idx)

class TransformerLM(nn.Module):
    """
    Basic Transformer for language modelling
    Parameters:
        * vocab_sz: int
        * dim: int - inner dimension of the model
        * depth: int (default: 6) 
        * heads: int (default: 8)
        * causal: bool (default: True) - if True does causal masking automatically
        * max_seq_len: int (default: 512)
        * tie_weights: bool - if True target embedding weights are used for computation output projection
    Inputs:
        * x - input ids, shape [bs, sl]
        * mask - optional boolean mask, shape [bs, sl]
    Returns:
        * logits - target token logits, shape [bs, sl, vocab_sz]
    """
    def __init__(self, vocab_sz, dim, depth=6, heads=8, causal=True,
                 max_seq_len=512, tie_weights=False,
                 attn_dropout=0.1, ff_dropout=0.1, emb_dropout=0.1):
        super().__init__()
        self.max_seq_len = max_seq_len
        self.emb = TransformerEmbedding(vocab_sz, dim, max_seq_len, dropout=emb_dropout)
        self.tfmr = TransformerEncoder(dim, depth, heads, causal=causal,
                                       attn_dropout=attn_dropout,
                                       ff_dropout=ff_dropout)
        self.proj = nn.Linear(dim, vocab_sz)
        if tie_weights: self.proj.weight = self.emb.emb.weight
        
    def forward(self, x, mask=None):
        x = self.emb(x)
        x = self.tfmr(x, mask=mask)
        return self.proj(x)