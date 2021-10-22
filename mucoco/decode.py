import logging
import math
import os
import sys
import torch
import numpy as np
import transformers


from transformers import AutoTokenizer, AutoConfig
from sentence_transformers import SentenceTransformer, util

from mucoco.utils import TargetProbability, Lambda, Optimizer, get_epsilon
import mucoco.losses as lossbuilder
import mucoco.options as options

import torch.nn.functional as F

logging.disable(logging.WARNING)
# logging.getLogger("transformers.GPT2ForSequenceClassification").disabled=True

def get_pareto_set(pareto_set, content_loss, style_loss, prediction):
    pareto_set.append((content_loss, style_loss, prediction))
    # print(len(pareto_set))
    if len(pareto_set) == 1:
        return [(content_loss, style_loss, prediction)]

    new_pareto_set = []
    for i in range(len(pareto_set)):
        l1, l2, pred = pareto_set[i]
        flag=False
        for j in range(len(pareto_set)):
            if i != j:
                l1j, l2j, predj = pareto_set[j]
                if l1j <= l1 and l2j <= l2 and (l1j < l1 or l2j < l2):
                    flag=True
                    break
        
        if not flag:
            new_pareto_set.append((l1, l2, pred))
    # print(new_pareto_set)
    return new_pareto_set


    # new_pareto_set = []
    # flag = False
    # for (l1, l2, pred) in pareto_set:
    #     if (content_loss <= l1) and (style_loss <= l2):
    #         if not flag:
    #             new_pareto_set.append((content_loss, style_loss, prediction))
    #             flag = True
    #     elif (content_loss <= l1) or (style_loss <= l2):
    #         new_pareto_set.append((l1, l2, pred))
    print(len(new_pareto_set))
    return new_pareto_set


def main(args):
    logging.basicConfig(
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        level=os.environ.get("LOGLEVEL", "INFO").upper(),
        stream=sys.stdout,
    )
    logger = logging.getLogger("mucoco")
    # logging.getLogger('AutoModelforSequenceClassification').setLevel(logging.ERROR)
    logger.info(args)

    if args.outfile is not None:
        outf = open(args.outfile, "w")
        # f2 = open(args.outfile + ".ref", "w")
        # fprefix = open(args.outfile + ".prefix", "w")

    # Fix seed for stochastic decoding
    if args.seed is not None:
        np.random.seed(args.seed)
        utils.set_torch_seed(args.seed)

    use_cuda = torch.cuda.is_available() and not args.cpu
    logger.info(
        "loading model(s) from {} and tokenizer(s) from {}".format(
            args.model, args.tokenizer
        )
    )

    name2tokenizer = {}
    name2model = {}
    name2config = {}
    loss2modelname = {}
    loss2tokenizer = {}
    embed_luts = []
    embed_scales = []

    betas = []
    model_paths = args.model.split(":")
    tokenizer_paths = args.tokenizer.split(":")

    if args.model_types is not None:
        model_types = args.model_types.split(":")
    else:
        model_types = [AutoModel for _ in model_paths]

    losses = args.loss.split(":")
    if args.lossabbr is not None:
        lossabbr = args.lossabbr.split(":")
    else:
        lossabbr = [x for x in losses]

    if args.label_id is None or args.label_id == "none":
        label_ids = [1 for _ in losses]
    else:
        label_ids = [int(i) for i in args.label_id.split(":")]
    
    if args.selection_criterion == "primary_allsat": 
        # with this flag, the output which minimized the primary objective while satisfying all objectives is selected. In case all constraints are not satisfied (e.g when constraints are competing or optimization fails), this will predict the default output (Using an autoregressive decoding setup: beam search in this case)
        betas = [1.0] + [0.0 for _ in range(len(losses)-1)]
    elif args.selection_criterion == "weighted_sum" and args.betas is not None:
        # this setup will select the best outputs according to the weights betas for each of the losses (even though they are not satisfied)
        betas = [float(beta) for beta in args.betas.split(":")]
    else:
        raise ValueError("correct selection_criterion or betas needs to be specified")

    assert len(betas) == len(losses) and len(losses) == len(model_paths) and len(model_paths) == len(model_types) and len(betas) == len(lossabbr)
    assert np.abs(sum(betas) - 1.0) < 1e-6, f"sum of betas is {sum(betas)} != 1.0"

    prev_vocab_size = None
    vocab_size = None
    primary_vocab_size = None

    #Load the models and tokenizers
    for i, model_path in enumerate(model_paths):
        if model_path not in name2model: #making sure we are not loading the model twice in case some constraints use the same model. 
            name2tokenizer[model_path] = AutoTokenizer.from_pretrained(tokenizer_paths[i], cache_dir=args.cache_dir,  use_fast=True)
            name2config[model_path] = AutoConfig.from_pretrained(model_path, cache_dir=args.cache_dir)

            if model_types[i] == "sentence-transformer":
                name2model[model_path] = SentenceTransformer(model_path)
            else:
                name2model[model_path] = getattr(transformers, model_types[i]).from_pretrained(model_path, config=name2config[model_path], cache_dir=args.cache_dir)
            
            if not args.show_warnings:
                logging.getLogger(name2model[model_path].__class__.__name__).setLevel(logging.ERROR) 
            
            name2model[model_path].eval()
            new_vocab_size = name2model[model_path].get_input_embeddings().num_embeddings
            if prev_vocab_size is None:
                vocab_size=new_vocab_size
            if new_vocab_size != prev_vocab_size and prev_vocab_size is not None:
                if not args.allow_diff_vocab:
                    raise ValueError(f"all models should have the same vocabulary {prev_vocab_size} != {vocab_size}")
                else:
                    logger.warning("all models don't have the same vocabulary and we are still proceeding")
            prev_vocab_size = vocab_size
        
        if i == 0:
            primary_vocab_size = vocab_size

        if args.target_tokenize_different: # for seq2seq models where target tokenizer is different than the source tokenizer
            embed_luts.append(name2model[model_path].get_decoder().get_input_embeddings())
        else:
            embed_luts.append(name2model[model_path].get_input_embeddings())
        
        if getattr(name2model[model_path], "get_decoder", None) is None: #this is for MarianMT models which have a weird embedding_scale parameter
            embed_scales.append(1.0)
        else:
            embed_scales.append(getattr(name2model[model_path].get_decoder(), "embed_scale", 1.0))
    
    if use_cuda:
        for name, model in name2model.items():
            model.cuda()
        logger.info("model(s) moved to GPU")
      
    #first loss is the primary loss, others are constraints
    lossfns = []
    for i, loss in enumerate(losses):
        lossfns.append(lossbuilder.build_loss(loss, name2model[model_paths[i]], name2tokenizer[model_paths[i]], args))
        loss2modelname[loss] = model_paths[i]
        loss2tokenizer[loss] = name2tokenizer[model_paths[i]]
    
    # if "metrics" in args.stopping_criterion:
    #     logger.info("Loading evaluation models")
    #     from stopping_criterion import transferability, acceptability, sts_similarity, wieting_sim

    logger.info("tokenizer(s), model(s) and loss function(s) loaded")

    if args.model_dtype == "fp16": #while this is supported it doesn't work that well yet. Not recommended
        for name, model in name2model.items():
            model.half()
        logger.info("changed everything to fp16")

    #constraint thresholds. In the paper, we recommend to start with a high threshold value which is usually satisfied by default or easily satisfied and then decrease it gradually, otherwise weird adversarial solutions come up. This code supports different kinds of schedules for decreasing this threshold (usually just step or linear suffices). If no schedule is specified, it just remains the same as the original. 
    if args.epsilons is not None and args.epsilons != "none":
        epsilons = [float(eps) for eps in args.epsilons.split(":")]
        if args.min_epsilons is not None:
            min_epsilons = [float(eps) for eps in args.min_epsilons.split(":")]
            epsilon_warmup_steps = [int(steps) for steps in args.epsilon_warmup_steps.split(":")]
            epsilon_cooldown_steps = [int(steps) for steps in args.epsilon_cooldown_steps.split(":")]
            epsilon_decay_functions = [f for f in args.epsilon_decay_functions.split(":")]
        else:
            min_epsilons = [float(eps) for eps in args.epsilons.split(":")]
            epsilon_warmup_steps = [1 for eps in min_epsilons]
            epsilon_cooldown_steps = [2 for eps in min_epsilons]
            epsilon_decay_functions = ["none" for eps in min_epsilons]
    else:
        epsilons = []
        min_epsilons = []
        decay_function = []
        epsilon_warmup_steps = []
        epsilon_cooldown_steps = []
    
    assert args.data is not None or args.additional_data is not None, "no data path has been provided"
    if args.data is not None:
        data_paths = args.data.split(":")
        if len(data_paths) == 1:
            source_data = data_paths[0]
            target_data = data_paths[0]
        else:
            source_data = data_paths[0]
            target_data = data_paths[1] #useful for debugging
    
        additional_data = args.additional_data
        if additional_data is None:
            additional_data = source_data #additional data was used in strap when x is paraphrased to z, then the model is used to generate y in the target style. If there's no additional_data, it defaults to the source text
    else:
        source_data = args.additional_data
        target_data = args.additional_data
        additional_data = args.additional_data
    
    logger.info("Loading the dataset ...")
    source_dataset = [l.strip() for l in open(source_data)]
    target_dataset = [l.strip() for l in open(target_data)]
    additional_dataset = [l.strip() for l in open(additional_data)]
    logger.info("Data loaded")

    source_batch, target_batch, additional_batch, for_predicted_source_batch, predicted_batch = [], [], [], [], []
    batch_size = args.batch_size # higher than 1 batch size does not work at the moment. It won't fit in a single GPU anyway 
    
    device = "cuda" if use_cuda else "cpu"
    c = 0

    losslists = [[] for _ in range(len(losses))]
    predictedlosslists = [[] for _ in range(len(losses))]
    source_primarylosslist = [] 
    # allparetosets = []
    all_stepcounts = []

    #data loading is very simple but probably can be sped up
    for source_text, target_text, additional_text in zip(source_dataset, target_dataset, additional_dataset):
        early_skip="n"
        if args.debug:
            early_skip = input(f"skip this example? {source_text} [yes(y)/maybe(m)/no(n)]")
            if early_skip == "y":
                continue

        if args.num_examples > 0 and c > 0 and c == args.num_examples: #stop after processing num_examples if it is set 
            print(f"done {c}")
            break
        c += 1

        primary_tokenizer = loss2tokenizer[losses[0]]
        source_indices = primary_tokenizer.encode(source_text, return_tensors="pt").to(device)
        additional_indices = primary_tokenizer.encode(additional_text, return_tensors="pt", add_special_tokens=False).to(device)

        eos_token_id=primary_tokenizer.eos_token_id
        if args.target_tokenize_different:
            with primary_tokenizer.as_target_tokenizer():
                eos_token_id=primary_tokenizer.eos_token_id
                
        predicted_indices = clean_output(name2model[model_paths[0]].generate(additional_indices, max_length=25, num_beams=args.beam_size)[0].tolist(), eos_token_id=eos_token_id, return_tensors=True)

        if args.target_tokenize_different:
            with primary_tokenizer.as_target_tokenizer():
                beam_prediction = primary_tokenizer.decode(predicted_indices[0].tolist())
        else:
            beam_prediction = primary_tokenizer.decode(predicted_indices[0].tolist())

        if not args.target_tokenize_different and "Seq2SeqLM" in model_paths[0]:
            logger.warning("you are using a seq2seq model for your primary loss but not tokenizing the target sentences with a different tokenizer.")

        #for_predicted_source_indices, are used to compute the primary loss wrt source as target. Useful for debugging style transfer models. 
        if args.target_tokenize_different:
            with primary_tokenizer.as_target_tokenizer():
                for_predicted_source_indices = primary_tokenizer.encode(source_text, return_tensors="pt").to(device)
                target_indices = primary_tokenizer.encode(target_text, return_tensors="pt", add_special_tokens=False).to(device)
        else:
            for_predicted_source_indices = source_indices
            target_indices = primary_tokenizer.encode(target_text, return_tensors="pt", add_special_tokens=False).to(device)
        
        source_batch.append(source_indices)
        target_batch.append(target_indices)
        for_predicted_source_batch.append(for_predicted_source_indices)
        predicted_batch.append(predicted_indices)
        additional_batch.append(additional_indices)

        if len(source_batch) == batch_size: #this is just one for now, greater than 1 batch size will not work

            source_batch = torch.cat(source_batch, dim=0).to(device)
            target_batch = torch.cat(target_batch, dim=0).to(device)
            additional_batch = torch.cat(additional_batch, dim=0).to(device)
            predicted_batch = torch.cat(predicted_batch, dim=0).to(device)
            for_predicted_source_batch = torch.cat(for_predicted_source_batch, dim=0).to(device)  

            skip=False
            predicted_allsat=False
            lengthwise_best_prediction = [None] * batch_size
            if predicted_batch is not None:
                # losses of the beam-search output: we should perform atleast well as this. If we don't, we predict this output
                # Also, we the beam-search output already satisfies the constraints, we skip mucoco
                predicted_labels = {}
                total_predicted_loss = 0.0
                for lossid in range(len(predictedlosslists)):
                    lossname = losses[lossid]
                    predicted_loss, predicted_lo =\
                        lossfns[lossid].compute_gold_loss(
                            (source_batch, predicted_batch), 
                            additional_batch=additional_batch, 
                            label_id=label_ids[lossid])

                    predicted_loss = predicted_loss.sum().item()
                    total_predicted_loss += betas[lossid] * predicted_loss
                    predicted_allsat=True
                    if lossid > 0:
                        predicted_allsat = predicted_allsat and (predicted_loss <= min_epsilons[lossid-1])
                    if "label_prediction" in predicted_lo:
                        predicted_labels[lossid] = predicted_lo['label_prediction']
                    else:
                        predicted_labels[lossid] = "NA"
                    predictedlosslists[lossid].append(predicted_loss)

                    if args.gold_loss_epsilons and lossid > 0: #use the predicted loss 
                        min_epsilons[lossid - 1] = predicted_loss
                
                lengthwise_best_prediction = [(beam_prediction, total_predicted_loss, predicted_allsat)]

            skip = predicted_allsat
            if args.debug and early_skip=="m": 
                print(f"new example: {source_text}\nautoregressive output: {beam_prediction}")
                for lossid in range(len(losses)):
                    print(f"{lossabbr[lossid]} for desired label_id({label_ids[lossid]}): {predictedlosslists[lossid][-1]}; predicted label: {predicted_labels[lossid]}")
                if predicted_allsat:
                    print(f"autoregressive output already satisfies the constraints")
                skip = input(f"skip this example? [y/n]")
                skip = skip == "y"
            
            if skip and predicted_allsat and not args.always_mucoco:
                print("the beam search output already satisfies all the constraints or there's no constraints. Skipping this example")
                if args.debug:
                    prediction_ids = ", ".join([str(idx) for idx in predicted_indices[0].tolist()])
                    prediction = beam_prediction
                    print(f"Prediction ids: {prediction_ids}")
                    print(f"Prediction: {prediction}")
                    print()
                
            if not skip:
                if args.max_length is None and args.init not in ["source", "target"]: 
                    #since we don't know the about length, we search in a (-length_diff, length_diff) window and predict the best performing one. 
                    predicted_length = predicted_batch.size(1)
                    length_range = range(max(1, predicted_length-args.length_diff), predicted_length+args.length_diff+1)
                    length_range = [x for x in length_range if x <= args.max_allowed_length and x >= 1]
                    print(predicted_length, length_range)
                    length_range = sorted(list(set(length_range)))
                else: 
                    #another way to use this approach is train models which also compute loss on <pad> token and then predict the entire sentence including pad, it has shown to work decently in some of our experiments
                    length_range = [args.max_length]

                for sent_length_ in length_range:
                    # prefix_length is used to indicate if instead of predicting the entire sentence via optimization, we want to fix a prefix (of specified length) and predict the remaining suffix. We use part of the beam search prediction as the prefix. 
                    if args.prefix_length > 0:
                        sent_length = sent_length - args.prefix_length
                        target_prefix = predicted_batch[:, :args.prefix_length]
                    else:
                        sent_length = sent_length_
                        target_prefix = torch.empty((source_indices.size(0), 0)).long().to(device)
                    
                    if sent_length <= 0:
                        continue
                    if sent_length >= args.max_allowed_length:
                        #max_allowed_length is just to make sure things don't go out of memory,
                        old_l = sent_length
                        sent_length = args.max_allowed_length
                        print(f"changed output length to {sent_length} from {old_l} to avoid GPU overflow. This is a temporary solution")
                    else:
                        print("predicting a sentence length: ", sent_length)
                        
                    if args.target_type == "simplex": # use V sized real vector for each token and apply softmax before output
                        outputs = TargetSimplex(
                            vocabsize=primary_vocab_size,
                            sent_length=sent_length,
                            batch_size=batch_size,
                            device=device,
                            temperature=args.decode_temperature,
                            st=args.st,
                            init_value=source_batch[:,1:-1] if args.init == "source" else None,
                            random_init=args.init == "random",
                            sampling_strategy=args.sampling_strategy,
                            sampling_strategy_k=args.sampling_strategy_k,
                            embed_scales=embed_scales
                        )
                    elif args.target_type == "probs": # use V sized vector which sums to one for each token and apply softmax before output
                        init_value = None
                        if args.init == "source": #initialize the target with the source
                            init_value = source_batch
                            target_prefix = torch.empty((source_indices.size(0), 0)).long().to(device)
                            sent_length = init_value.size(1)
                            # print(source_batch, init_value, sent_length, init_value)
                        elif args.init == "target": #initialize the target with the autoregressive output
                            init_value = target_batch
                            target_prefix = torch.empty((source_indices.size(0), 0)).long().to(device)
                            sent_length = init_value.size(1)
                            # print(source_batch, init_value)
                        
                        outputs = TargetProbability(
                            vocabsize=primary_vocab_size,
                            sent_length=sent_length,
                            batch_size=batch_size,
                            device=device,
                            st=args.st,
                            init_value=init_value,
                            random_init=args.init == "random",
                            sampling_strategy=args.sampling_strategy,
                            sampling_strategy_k=args.sampling_strategy_k,
                            embed_scales=embed_scales
                        )

                    if len(losses) > 1:
                        lambda_ = Lambda(count=len(epsilons))
                        if use_cuda:
                            lambda_.cuda()

                    optimizer = Optimizer.from_opt(outputs, args)
                    if len(losses) > 1:
                        old_optim = args.optim
                        args.optim = "ascentsgd"
                        old_lr = args.lr
                        args.lr = args.lambda_lr
                        optimizer_lambda = Optimizer.from_opt(lambda_, args)
                        args.optim = old_optim
                        args.lr = old_lr

                    best_loss = [None] * batch_size
                    best_allsat = [None] * batch_size
                    best_losses = [[None] * batch_size for _ in range(len(losses))]
                    
                    best_pred_tokens = [None] * batch_size
                    best_prediction_set = [set() for _ in range(batch_size)]
                    best_pred_probs = [None] * batch_size
                    best_index = [-1 for i in range(batch_size)]
                    
                    scaler = None
                    if args.model_dtype == "fp16" and args.fp16_source == "pytorch":
                        scaler = torch.cuda.amp.GradScaler()
                
                
                    for lossid, lossname in enumerate(losses):
                        losslists[lossid].append([])

                    for step in range(args.optim_steps):
                        try:
                            with torch.cuda.amp.autocast():
                                losses_for_backward = []
                                logging_outputs = []
                                pred_embeds, pred_tokens, pred_probs = outputs.forward_multiple(embed_luts)  # forward
                                print(len(pred_embeds[0]))
                                print(len(embed_luts))

                                for lossid, lossname in enumerate(losses):
                                    lossvalue, logging_output =\
                                        lossfns[lossid].compute_loss(
                                            [source_batch, target_prefix], 
                                            [pred_tokens, pred_embeds[0][lossid], pred_probs], 
                                            additional_batch=additional_batch, 
                                            embed_scale=embed_scales[lossid], 
                                            label_id=label_ids[lossid]
                                        )

                                    losslists[lossid][-1].append(lossvalue.sum().item())  #for logging
                                    losses_for_backward.append(lossvalue)  # for backward
                                    logging_outputs.append(logging_output)
                                
                                optimizer.zero_grad(set_to_none=True)
                                outputs.zero_grad()
                                if len(losses) > 1:
                                    optimizer_lambda.zero_grad(set_to_none=True)
                                    lambda_.zero_grad()

                                for model in name2model.values():
                                    model.zero_grad()
                                
                                if args.linear_scale: # no lagragian, plain old linear sum
                                    # total_loss = betas[0] * losses_for_backward[0]
                                    total_loss = 0
                                    cur_epsilons = [] # just for avoiding syntax errors, epsilons are useless in this setting
                                    for sid in range(len(losses_for_backward)):
                                        total_loss = total_loss + betas[i] * losses_for_backward[0]
                                        cur_epsilons.append(0.0)
                                else:
                                    total_loss = losses_for_backward[0]
                                    total_loss_for_lambda = 0.0
                                    cur_epsilons = []
                                    for sid in range(1, len(losses_for_backward)): #the secondary losses or constraints
                                        cur_epsilon = get_epsilon(step, epsilons[sid-1], min_epsilons[sid-1], epsilon_warmup_steps[sid-1], epsilon_cooldown_steps[i-1], epsilon_decay_functions[sid-1])

                                        damp = args.dampness * (cur_epsilon - losses_for_backward[sid]).detach()
                                        closs_for_theta = lambda_.get_loss(sid - 1, damp, torch.clamp(cur_epsilon - losses_for_backward[sid], max=0.0))
                                        closs_for_lambda = lambda_.get_loss(sid - 1, damp, cur_epsilon - losses_for_backward[sid])
                                        
                                        total_loss = total_loss - closs_for_theta
                                        total_loss_for_lambda = total_loss_for_lambda - closs_for_lambda
                                        cur_epsilons.append(cur_epsilon)                                    
                                
                                total_batchloss = total_loss.sum()
                            
                            optimizer.backward(total_batchloss, retain_graph=True, scaler=scaler)
                            # if args.debug:
                            #     total_norm = 0
                            #     gi=0
                            #     for p in outputs.parameters():
                            #         gi+=1
                            #         param_norm = p.grad.data.norm(2, -1).sum(dim=0)
                            #         print("for theta", param_norm)
                            optimizer.step(scaler=scaler)

                            if len(losses) > 1 and not args.linear_scale:
                                total_batchloss_for_lambda = total_loss_for_lambda.sum()
                                optimizer_lambda.backward(total_batchloss_for_lambda, retain_graph=True, scaler=scaler)
                                optimizer_lambda.step()
                                lambda_.make_positive()
                                # if args.debug:
                                #     total_norm = 0
                                #     gi=0
                                #     for p in lambda_.parameters():
                                #         gi+=1
                                #         param_norm = p.grad.data.norm(2, -1).sum(dim=0)
                                #         print("for lambda", param_norm)
                            
                            def get_sent(tokens, tokenizer):
                                batch = []
                                if args.target_tokenize_different:
                                    with tokenizer.as_target_tokenizer():
                                        for toks in tokens:
                                            batch.append(tokenizer.decode(clean_output(toks.tolist(), -1)))
                                else:
                                    for toks in tokens:
                                        batch.append(tokenizer.decode(clean_output(toks.tolist(), -1)))
                                return batch
                            
                            if args.debug:
                                target_sents = get_sent(torch.cat([target_prefix, pred_tokens], dim=1), primary_tokenizer)
                                print(target_sents)
                            
                            cur_losses = []
                            for b in range(batch_size):
                                cur_loss = 0.0
                                for beta, lossval in zip(betas, losses_for_backward):
                                    cur_loss = cur_loss + beta * lossval[b].item()     
                                cur_losses.append(cur_loss)
                                
                                constrained = []
                                allsat = True
                                for i in range(1, len(losses)):
                                    if losses_for_backward[i] <= min_epsilons[i - 1]:
                                        constrained.append("sat")
                                    else:
                                        constrained.append("vio")
                                        allsat=False
                                
                                if args.show_all_outputs and len(losses) > 1 and allsat:
                                    best_prediction_set[b].add(target_sents[b])
                                    
                                constrained = ",".join(constrained)

                                if best_loss[b] is None or (best_loss[b] > cur_loss):
                                    # if args.selection_criterion != "primary_allsat" or allsat:
                                    best_loss[b] = cur_loss
                                    best_allsat[b] = allsat
                                    for i in range(len(losses)):
                                        best_losses[i][b] = losses_for_backward[i][b].item()
                                    
                                    best_pred_tokens[b] = pred_tokens[b]
                                    best_index[b] = step
                                    best_pred_probs[b] = (pred_probs[b].cpu(), logging_outputs[0]["lm_logprobs"][b])
                                    best_constrained = constrained
                                    
                            if (step) % args.log_interval == 0:
                                if len(losses) > 1:
                                    log = f"Step {step}: loss:{total_batchloss:.4f}; current [loss:{sum(cur_losses):.4f}; l:{','.join([f'{x:.4f}' for x in lambda_().tolist()])}; e:{','.join([f'{x:.4f}' for x in cur_epsilons])}; cons:{constrained}; "
                                    for i in range(len(losslists)):
                                        log = log + f" {lossabbr[i]}:{losslists[i][-1][-1]:.4f}; "
                                    
                                    log = log[:-1] + f"] best [cur_loss:{sum(best_loss):.4f}; cons:{best_constrained};  "
                                    for i in range(len(best_losses)):
                                        log = log + f"{lossabbr[i]}:{sum(best_losses[i]):.4f}; "
                                    log = log[:-1] + f"@ step #{best_index[-1]}" 
                                    log = log + "]"
                                    print(log)
                                else:
                                    log = f"Step {step}: loss:{total_batchloss:.4f}; current [loss:{sum(cur_losses):.4f}; "
                                    for i in range(len(losslists)):
                                        log = log + f" {lossabbr[i]}:{losslists[i][-1][-1]:.4f}; "
                                    
                                    log = log[:-1] + f"] best [cur_loss:{sum(best_loss):.4f} "
                                    for i in range(len(best_losses)):
                                        log = log + f"{lossabbr[i]}:{sum(best_losses[i]):.4f}; "
                                    log = log[:-1] + f" at step {best_index[-1]}" 
                                    log = log + "]"
                                    print(log)
                            
                            del losses_for_backward
                        except KeyboardInterrupt:
                            print("skipping remaining optimizing steps and showing the best option so far")
                            break

                    predictions = []
                    prediction_idss = []
                    for b, item in enumerate(best_pred_tokens):
                        if not best_allsat[b]:
                            prediction_ids = ", ".join([str(idx) for idx in predicted_indices[0].tolist()])
                            prediction = beam_prediction
                            lossvalue = predictedlosslists[b][0]
                            print("best prediction is from beam search, all constraints were not satisfied")
                        else:
                            prediction_ids = ", ".join([str(x) for x in target_prefix[b].tolist()])
                            prediction_ids +=   f'[{", ".join([str(x) for x in item.tolist()])}]'
                            if args.target_tokenize_different:
                                with primary_tokenizer.as_target_tokenizer():
                                    prediction = primary_tokenizer.decode(target_prefix[b].tolist()) + ' ' + primary_tokenizer.decode(item.tolist())
                            else:
                                targets = clean_output(item.tolist(), primary_tokenizer.eos_token_id)
                                prediction = primary_tokenizer.decode(targets) + " " + primary_tokenizer.decode(item.tolist())
                            print("best prediction at step",best_index[b])
                            lossvalue = best_loss[b]
                        prediction_idss.append(prediction_ids)
                        predictions.append(prediction)

                        modify_condition = True 
                        modify_condition =\
                            modify_condition or\
                            lengthwise_best_prediction[b] is None or\
                            (self.selection_criterion == "primary_allsat" and not lengthwise_best_prediction[b][2] and best_allsat[b]) or\
                            (self.selection_criterion == "primary_allsat" and lengthwise_best_prediction[b][2] and best_allsat[b] and lengthwise_best_prediction[b][1] > lossvalue) or\
                            (self.selection_criterion == "weighted_sum" and lengthwise_best_prediction[b][1] > lossvalue)
                        
                        if modify_condition:
                            lengthwise_best_prediction[b] = (prediction, lossvalue, best_allsat[b])

                    if args.debug:                    
                        for i, item in enumerate(best_pred_tokens):
                            print(f"predicting length: {sent_length}")
                            print("Given source:", source_text)
                            print("Given target: ", target_text)
                            print("Given additional: ", additional_text)
                            print(f"Prediction ids: {prediction_ids}")
                            print(f"Prediction: {prediction}")
                            print("All generations that satisfied the constraints: ", best_prediction_set[i])

                            out = []
                            # print(predictedlosslists)
                            # input()
                            # if target_batch is not None:
                            #     for lossid in range(len(losses)):
                            #         out.append(f"Gold {lossabbr[lossid]}: {predictedlosslists[lossid][-1]}")
                            #out.append(f"Source {lossabbr[0]}: {source_primarylosslist[-1]}")
                            # print("; ".join(out))

                            out = []
                            for lossid in range(len(losses)):
                                out.append(f"{losses[lossid]}: {best_losses[lossid][i]}")
                            print("; ".join(out))
                    all_stepcounts += best_index
                
                optimizer.zero_grad()
                del outputs
                del optimizer
                if len(losses) > 1:
                    optimizer_lambda.zero_grad()
                    del optimizer_lambda
                    del lambda_
                for modelname in loss2modelname.values():
                    name2model[modelname].zero_grad() 
                torch.cuda.empty_cache()

            
            if args.debug:
                print("best prediction: ", lengthwise_best_prediction[b][0].strip().replace("\n", " ") + "\n")
            else:   
                for b, item in enumerate(best_pred_tokens):
                    f.write(lengthwise_best_prediction[b][0].strip().replace("\n", " ") + "\n")
                    f.flush()

            del source_batch
            del target_batch
            del additional_batch
            del for_predicted_source_batch
            del predicted_batch
            source_batch = []
            target_batch = []
            for_predicted_source_batch = []
            additional_batch = []
            predicted_batch = []
    f.close()
    print("average numbers of steps to converge =", np.mean(all_stepcounts))

def clean_output(tokens, eos_token_id, return_tensors=False):
    new_tokens = []
    for tok in tokens:
        if tok != eos_token_id:
            new_tokens.append(tok)
        else:
            break
    if return_tensors:
        return torch.LongTensor([new_tokens])
    return new_tokens
    
def cli_main():
    parser = options.get_parser()
    args = parser.parse_args()
    main(args)
