import torch.nn as nn
import torch

import torch.nn.functional as F


def _get_scores(predict_emb, target_embedding):
    return predict_emb.matmul(target_embedding.weight.t())

def top_k_top_p_filtering(logits, top_k=0, top_p=0.0, filter_value=-float('Inf')):
    """ Filter a distribution of logits using top-k and/or nucleus (top-p) filtering
        Args:
            logits: logits distribution shape (vocabulary size)
            top_k >0: keep only top k tokens with highest probability (top-k filtering).
            top_p >0.0: keep the top tokens with cumulative probability >= top_p (nucleus filtering).
                Nucleus filtering is described in Holtzman et al. (http://arxiv.org/abs/1904.09751)
    """
    logits = logits.clone().detach()
    for i in range(logits.size(0)):
        for j  in range(logits.size(1)):
            top_k = min(top_k, logits[i, j].size(-1))  # Safety check
            if top_k > 0:
                # Remove all tokens with a probability less than the last token of the top-k
                indices_to_remove = logits[i, j] < torch.topk(logits[i, j], top_k)[0][..., -1, None]
                logits[i, j, indices_to_remove] = filter_value

            if top_p > 0.0:
                sorted_logits, sorted_indices = torch.sort(logits[i, j], descending=True)
                cumulative_probs = torch.cumsum(F.softmax(logits[i, j], dim=-1), dim=-1)

                # Remove tokens with cumulative probability above the threshold
                sorted_indices_to_remove = cumulative_probs > top_p
                # Shift the indices to the right to keep also the first token above the threshold
                sorted_indices_to_remove[..., 1:] = sorted_indices_to_remove[..., :-1].clone()
                sorted_indices_to_remove[..., 0] = 0

                indices_to_remove = sorted_indices[sorted_indices_to_remove]
                logits[i, j, indices_to_remove] = filter_value
    return logits

class TargetSimplex(nn.Module):
    def __init__(
        self,
        vocabsize,
        sent_length,
        batch_size,
        device,
        temperature=1.0,
        st=False,
        init_value=None,
        random_init=False,
        sampling_strategy="argmax",
        sampling_strategy_k = 0,
        embed_scales=None
    ):
        super(TargetSimplex, self).__init__()
        # special = torch.Tensor(batch_size, sent_length, 3).fill_(-1000)
        # special.requires_grad=False
        # self._pred_logiprobnn.Parameter(
            # torch.cat([special, torch.Tensor(batch_size, sent_length, vocabsize-3)], dim=-1).to(device)
        # )
        self._pred_logits = nn.Parameter(torch.Tensor(batch_size, sent_length, vocabsize).to(device))
        self.special_mask = torch.ones_like(self._pred_logits)
        self.temperature = temperature
        self.initialize(random_init=random_init, init_value=init_value)
        self.device = device
        self.st = st
        self.sampling_strategy = sampling_strategy
        self.sampling_strategy_k = sampling_strategy_k
        self.embed_scales = embed_scales
    
    # def sanitize(self, tokenizer):
    #     # this function reduces the probability of illegal tokens like <s> and other stuff to  not have a repeated sequence of </s>
    #     self._pred_logits[:,:, tokenizer.bos_token_id] = -1000.0
    #     self._pred_logits[:,:, tokenizer.eos_token_id] = -1000.0

        

    def forward(self, content_embed_lut, style_embed_lut=None, tokenizer=None, debug=False):
        
        # self.sanitize(tokenizer)
        # self.special_mask[:, :, :3] = 0.
        _, index = (self._pred_logits * self.special_mask).max(-1, keepdim=True)
        # print(index.size())
        predictions = index.squeeze(-1)
        # print(predictions.size())
        # input()

        if self.temperature == 0: # no softmax, special case, doesn't work don't use
            pred_probs = self._pred_logits
        else:    
            pred_probs = F.softmax(self._pred_logits / self.temperature, dim=-1)
        
        softmax_pred_probs = pred_probs
        if self.st:
            y_hard = torch.zeros_like(pred_probs).scatter_(-1, index, 1.0)
            pred_probs = y_hard - pred_probs.detach() + pred_probs

        if debug:
            print(softmax_pred_probs.max(dim=-1))
            print(softmax_pred_probs.gather(-1, index))
            print(self._pred_logits.gather(-1, index))
            print(torch.exp(self._pred_logits).eq(1.).all())
            print(index)
            input()

        source_pred_emb = (pred_probs.unsqueeze(-1) * content_embed_lut.weight).sum(dim=-2)
        target_pred_emb = None
        if style_embed_lut is not None:
            target_pred_emb = (pred_probs.unsqueeze(-1) * style_embed_lut.weight).sum(dim=-2)
        return (source_pred_emb, target_pred_emb, softmax_pred_probs), predictions, pred_probs
    

    def forward_multiple(self, embed_luts):
        if self.temperature == 0: # no softmax, special case, doesn't work don't use
            pred_probs = self._pred_logits
        else:    
            pred_probs = F.softmax(self._pred_logits / self.temperature, dim=-1)

        logits = self._pred_logits
        if self.sampling_strategy == "greedy":

            _, index = (logits * self.special_mask).max(-1, keepdim=True)
            predictions = index.squeeze(-1)
        elif self.sampling_strategy.startswith("topk"):
            top_k = int(self.sampling_strategy_k)
            filtered_logits = top_k_top_p_filtering(logits, top_k=top_k, top_p=0)
            probabilities = F.softmax(filtered_logits, dim=-1)
            index = torch.multinomial(probabilities.view(-1, filtered_logits.size(-1)), 1)
            index = index.view(filtered_logits.size(0), filtered_logits.size(1), -1)
            predictions = index.squeeze(-1)
        elif self.sampling_strategy.startswith("topp"):
            top_p = float(self.sampling_strategy_k)
            filtered_logits = top_k_top_p_filtering(logits, top_k=0, top_p=top_p)
            probabilities = F.softmax(filtered_logits, dim=-1)
            index = torch.multinomial(probabilities.view(-1, filtered_logits.size(-1)), 1)
            index = index.view(filtered_logits.size(0), filtered_logits.size(1), -1)
            predictions = index.squeeze(-1)
        else:
            raise ValueError("wrong decode method. If you want to do beam search, change the objective function")
        
        softmax_pred_probs = pred_probs
        if self.st:
            y_hard = torch.zeros_like(pred_probs).scatter_(-1, index, 1.0)
            pred_probs = y_hard - pred_probs.detach() + pred_probs
        
        pred_embs = []
        for embed_lut, embed_scale in zip(embed_luts, self.embed_scales):
            pred_embs.append((pred_probs.unsqueeze(-1) * embed_lut.weight).sum(dim=-2))
        
        return (pred_embs, ), predictions, (pred_probs, softmax_pred_probs) #pred_probs is actually just logits


    def initialize(self, random_init=False, init_value=None):
        if init_value is not None:
            # print(init_value.size())
            eps = 0.9999
            V = self._pred_logits.size(2)
            print(V)
            print(1-eps-eps/(V-1))
            print(eps/(V-1))
            init_value = torch.zeros_like(self._pred_logits).scatter_(-1, init_value.unsqueeze(2), 1.0-eps-eps/(V-1))
            # init_value = torch.log(init_value + eps/(V-1))
            self._pred_logits.data.copy_(init_value.data)
        elif random_init:
            torch.nn.init.uniform_(self._pred_logits, 1e-6, 1e-6)
        else:
            torch.nn.init.zeros_(self._pred_logits)
            # init_value = torch.empty_like(self._pred_logits).fill_(0.)
            # self._pred_logits.data.copy_(init_value.data)

    @classmethod
    def decode_beam(cls, pred_probs, model, embed_lut, prefix, device, beam_size=1):
        answers = []
        # pred_probs = F.softmax(self._pred_logits / self.temperature, dim=-1)
        print(pred_probs.unsqueeze(-1).size())
        pred_emb = (pred_probs.unsqueeze(-1) * embed_lut.weight).sum(dim=-2)
        print(pred_emb.size())
        prefix_emb = embed_lut(prefix)

        _, topk_words = pred_probs.topk(2 * beam_size, dim=-1)
        print(topk_words.size())
        for b in range(pred_probs.size(0)):
            beam = torch.empty((0, beam_size)).long().to(device)
            beam_emb = torch.empty((0, beam_size, embed_lut.weight.size(1))).to(device)

            for i in range(pred_probs.size(1)):
                cand = embed_lut(topk_words[b, i : i + 1])
                print(cand.size())
                input_emb = torch.cat(
                    [
                        prefix_emb.repeat(2 * beam_size, 1, 1),
                        beam_emb.repeat(2, 1, 1),
                        cand,
                        pred_emb[b, i:-1, :].repeat(2 * beam_size, 1, 1),
                    ],
                    dim=1,
                )
                print(input_emb)

                # feed into model and get scores
                model_output = model(inputs_embeds=input_emb)
                lm_logits = model_output[0]
                lm_logprobs = F.log_softmax(lm_logits, dim=-1)

                print(lm_logprobs.size())
                print(beam.size())
                print(pred_probs.size())
                input()
                # compute nll loss
                # prefix

                # might have to transpose
                loss = F.nll_loss(
                    lm_logprobs[:, : prefix.size(1) - 1, :].squeeze(0),
                    prefix[:, 1 : prefix.size(1)].squeeze(0),
                    reduction="none",
                )

                loss += F.nll_loss(
                    lm_logprobs[:, prefix.size(1) - 1 : prefix.size() + b, :].squeeze(
                        0
                    ),
                    torch.cat(
                        [
                            prefix[:, prefix.size() - 1 :],
                            beam.repeat(2, 1),
                            topk_words[b : i : i + 1],
                        ],
                        dim=1,
                    ),
                )

                # suffix
                suffix_loss = (
                    pred_probs[b, i + 1 :, :].unsqueeze() * lm_logprobs[i:-1, :]
                ).sum(dim=-1)
                print(suffix_loss.size())

                # sort beam
                _, beam = torch.topk(-loss, dim=-1)

            answer.append(beam[0])

        return answer


class TargetProbability(nn.Module): #this is the class used in the final results
    def __init__(
        self,
        vocabsize,
        sent_length,
        batch_size,
        device,
        st=False,
        init_value=None,
        random_init=False,
        sampling_strategy="argmax",
        sampling_strategy_k = 0,
        embed_scales=None
    ):
        super(TargetProbability, self).__init__()
        self._pred_probs = nn.Parameter(torch.Tensor(batch_size, sent_length, vocabsize).to(device))
        self.initialize(random_init=random_init, init_value=init_value)
        self.device = device
        self.st = st #straight-through or not
        self.sampling_strategy = sampling_strategy
        self.sampling_strategy_k = sampling_strategy_k   
        self.embed_scales = embed_scales      

    def forward_multiple(self, embed_luts):
        
        if self.embed_scales is None:
            embed_scales = [1.0 for i in embed_luts]

        pred_probs = self._pred_probs
        if self.sampling_strategy == "greedy":
            _, index = pred_probs.max(-1, keepdim=True)
            predictions = index.squeeze(-1)
        elif self.sampling_strategy == "notpad": #top-1 might be a <pad> token, this ensure pad is never sampled
            _, index = pred_probs.topk(-1, k=2, keepdim=True)
            predictions = index.squeeze(-1)
        else:
            raise ValueError("wrong sampling strategy")
        
        softmax_pred_probs = pred_probs
        if self.st:
            y_hard = torch.zeros_like(pred_probs).scatter_(-1, index, 1.0)
            pred_probs = y_hard - pred_probs.detach() + pred_probs
        
        pred_embs = []
        for embed_lut, embed_scale in zip(embed_luts, self.embed_scales):
            if embed_lut.weight.size(0) > pred_probs.size(2):
                pred_embs.append((pred_probs.unsqueeze(-1) * embed_lut.weight[:pred_probs.size(2), :]).sum(dim=-2))
            elif embed_lut.weight.size(0) < pred_probs.size(2):
                pred_embs.append((pred_probs[:, :, :embed_lut.weight.size(0)].unsqueeze(-1) * embed_lut.weight).sum(dim=-2))
            else:
                pred_embs.append((pred_probs.unsqueeze(-1) * embed_lut.weight).sum(dim=-2))
        
        return (pred_embs, ), predictions, (pred_probs, softmax_pred_probs) #pred_probs is actually just logits


    def initialize(self, random_init=False, init_value=None):
        if init_value is not None:
            eps = 0.999
            V = self._pred_probs.size(2)
            init_value_ = torch.zeros_like(self._pred_probs).fill_(eps/(V-1))
            init_value_ = init_value_.scatter_(-1, init_value.unsqueeze(2), 1.0-eps)
            self._pred_probs.data.copy_(init_value_.data)
        elif random_init: #sample a simplex from a dirichlet distribution for each token probability
            p = torch.distributions.dirichlet.Dirichlet(10000 * torch.ones(self._pred_probs.size(-1)))
            init_value = torch.empty_like(self._pred_probs)
            for i in range(self._pred_probs.size(0)):
                for j in range(self._pred_probs.size(1)):
                    init_value[i, j] = p.sample()
            self._pred_probs.data.copy_(init_value.data) 
        else: # uniform
            torch.nn.init.ones_(self._pred_probs)
            self._pred_probs.data.div_(self._pred_probs.data.sum(dim=-1, keepdims=True))