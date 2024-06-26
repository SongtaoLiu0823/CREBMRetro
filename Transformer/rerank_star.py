import torch
import torch.nn as nn
import logging
import argparse
import random
import numpy as np
import os
import json
import pandas as pd

from tqdm import trange
from copy import deepcopy
from rdkit import Chem
from rdkit.Chem import AllChem
from preprocess import get_vocab_size, get_char_to_ix, get_ix_to_char
from modeling import TransformerConfig, Transformer, get_products_mask, get_reactants_mask, get_mutual_mask
from rdkit.rdBase import DisableLog
from reward_model import RewardTransformerConfig, RewardTransformer, get_input_mask_reward, get_output_mask_reward, get_mutual_mask_reward

DisableLog('rdApp.warning')


class ValueMLP(nn.Module):
    def __init__(self, n_layers, fp_dim, latent_dim, dropout_rate):
        super(ValueMLP, self).__init__()
        self.n_layers = n_layers
        self.fp_dim = fp_dim
        self.latent_dim = latent_dim
        self.dropout_rate = dropout_rate

        logging.info('Initializing value model: latent_dim=%d' % self.latent_dim)

        layers = []
        layers.append(nn.Linear(fp_dim, latent_dim))
        # layers.append(nn.BatchNorm1d(latent_dim,
        #                              track_running_stats=False))
        layers.append(nn.ReLU())
        layers.append(nn.Dropout(self.dropout_rate))
        for _ in range(self.n_layers - 1):
            layers.append(nn.Linear(latent_dim, latent_dim))
            # layers.append(nn.BatchNorm1d(latent_dim,
            #                              track_running_stats=False))
            layers.append(nn.ReLU())
            layers.append(nn.Dropout(self.dropout_rate))
        layers.append(nn.Linear(latent_dim, 1))

        self.layers = nn.Sequential(*layers)

    def forward(self, fps):
        x = fps
        x = self.layers(x)
        x = torch.log(1 + torch.exp(x))

        return x


def smiles_to_fp(s, fp_dim=2048, pack=False):
    mol = Chem.MolFromSmiles(s)
    fp = AllChem.GetMorganFingerprintAsBitVect(mol, 2, nBits=fp_dim)
    onbits = list(fp.GetOnBits())
    arr = np.zeros(fp.GetNumBits(), dtype=np.bool)
    arr[onbits] = 1

    if pack:
        arr = np.packbits(arr)
    fp = 1 * np.array(arr)

    return fp


def value_fn(smi):
    fp = smiles_to_fp(smi, fp_dim=args.fp_dim).reshape(1,-1)
    fp = torch.FloatTensor(fp).to(device)
    v = value_model(fp).item()
    return v


def convert_symbols_to_inputs(products_list, reactants_list, max_length):
    num_samples = len(products_list)
    #products
    products_input_ids = torch.zeros((num_samples, max_length), device=device, dtype=torch.long)
    products_input_mask = torch.zeros((num_samples, max_length), device=device)

    #reactants
    reactants_input_ids = torch.zeros((num_samples, max_length), device=device, dtype=torch.long)
    reactants_input_mask = torch.zeros((num_samples, max_length), device=device)

    for cnt in range(num_samples):
        products = '^' + products_list[cnt] + '$'
        reactants = '^' + reactants_list[cnt] + '$'
        
        for i, symbol in enumerate(products):
            products_input_ids[cnt, i] = char_to_ix[symbol]
        products_input_mask[cnt, :len(products)] = 1

        for i in range(len(reactants)-1):
            reactants_input_ids[cnt, i] = char_to_ix[reactants[i]]
        reactants_input_mask[cnt, :len(reactants)-1] = 1
    return (products_input_ids, products_input_mask, reactants_input_ids, reactants_input_mask)


def cano_smiles(smiles):
    try:
        tmp = Chem.MolFromSmiles(smiles)
        if tmp is None:
            return None, smiles
        tmp = Chem.RemoveHs(tmp)
        if tmp is None:
            return None, smiles
        [a.ClearProp('molAtomMapNumber') for a in tmp.GetAtoms()]
        return tmp, Chem.MolToSmiles(tmp)
    except:
        return None, smiles


def get_output_probs(product, res):
    test_products_ids, test_products_mask, test_res_ids, test_res_mask = convert_symbols_to_inputs([product], [res], args.max_length)
    # To Tensor
    test_mutual_mask = get_mutual_mask([test_res_mask, test_products_mask])
    test_products_mask = get_products_mask(test_products_mask)
    test_res_mask = get_reactants_mask(test_res_mask)

    logits = predict_model(test_products_ids, test_res_ids, test_products_mask, test_res_mask, test_mutual_mask)
    prob = logits[0, len(res), :] / args.temperature
    prob = torch.exp(prob) / torch.sum(torch.exp(prob))
    return prob.detach()


def get_beam(product, beam_size):
    lines = []
    scores = []
    final_beams = []
    object_size = beam_size

    for i in range(object_size):
        lines.append("")
        scores.append(0.0)

    for step in range(args.max_length):
        if step == 0:
            prob = get_output_probs(product, "")
            result = torch.zeros((vocab_size, 2), device=device)
            for i in range(vocab_size):
                result[i, 0] = -torch.log10(prob[i])
                result[i, 1] = i
        else:
            num_candidate = len(lines)
            result = torch.zeros((num_candidate * vocab_size, 2), device=device)
            for i in range(num_candidate):
                prob = get_output_probs(product, lines[i])
                for j in range(vocab_size):
                    result[i*vocab_size+j, 0] = -torch.log10(prob[j]) + scores[i]
                    result[i*vocab_size+j, 1] = i * 100 + j

        ranked_result = result[result[:, 0].argsort()]

        new_beams = []
        new_scores = []
        
        if len(lines) == 0:
            break

        for i in range(object_size):
            symbol = ix_to_char[ranked_result[i, 1].item()%100]
            beam_index = int(ranked_result[i, 1]) // 100

            if symbol == '$':
                added = lines[beam_index] + symbol
                if added != "$":
                    final_beams.append([lines[beam_index] + symbol, ranked_result[i,0]])
                object_size -= 1
            else:
                new_beams.append(lines[beam_index] + symbol)
                new_scores.append(ranked_result[i, 0])

        lines = new_beams
        scores = new_scores

        if len(lines) == 0:
            break

    for i in range(len(final_beams)):
        final_beams[i][1] = final_beams[i][1] / len(final_beams[i][0])

    final_beams = list(sorted(final_beams, key=lambda x:x[1]))
    answer = []
    aim_size = beam_size
    for k in range(len(final_beams)):
        if aim_size == 0:
            break
        reactants = set(final_beams[k][0].split("."))
        num_valid_reactant = 0
        sms = set()
        for r in reactants:
            r = r.replace("$", "")
            m = Chem.MolFromSmiles(r)
            if m is not None:
                num_valid_reactant += 1
                sms.add(Chem.MolToSmiles(m))
        if num_valid_reactant != len(reactants):
            continue
        if len(sms):
            answer.append([sorted(list(sms)), final_beams[k][1]])
            aim_size -= 1
    return answer


def load_dataset(split):
    file_name = "%s_dataset.json" % split
    file_name = os.path.expanduser(file_name)
    dataset = [] # (product_smiles, materials_smiles, depth)
    with open(file_name, 'r') as f:
        _dataset = json.load(f)
        for _, reaction_trees in _dataset.items():
            product = reaction_trees['1']['retro_routes'][0][0].split('>')[0]
            product_mol = Chem.MolFromInchi(Chem.MolToInchi(Chem.MolFromSmiles(product)))
            product = Chem.MolToSmiles(product_mol)
            _, product = cano_smiles(product)
            materials_list = []
            for i in range(1, int(reaction_trees['num_reaction_trees'])+1):
                materials_list.append(reaction_trees[str(i)]['materials'])
            dataset.append({
                "product": product,
                "targets": materials_list, 
                "depth": reaction_trees['depth']
            })

    return dataset

def convert_symbols_to_inputs_reward(input_list, output_list, max_length):
    num_samples = len(input_list)
    #input
    input_ids = np.zeros((num_samples, max_length))
    input_mask = np.zeros((num_samples, max_length))

    #output
    output_ids = np.zeros((num_samples, max_length))
    output_mask = np.zeros((num_samples, max_length))

    #for output
    token_ids = np.zeros((num_samples, max_length))
    token_mask = np.zeros((num_samples, max_length))

    for cnt in range(num_samples):
        input_ = '^' + input_list[cnt] + '$'
        output_ = '^' + output_list[cnt] + '$'
        
        for i, symbol in enumerate(input_):
            input_ids[cnt, i] = char_to_ix[symbol]
        input_mask[cnt, :len(input_)] = 1

        for i in range(len(output_)-1):
            output_ids[cnt, i] = char_to_ix[output_[i]]
            token_ids[cnt, i] = char_to_ix[output_[i+1]]
            if i != len(output_)-2:
                token_mask[cnt, i] = 1
        output_mask[cnt, :len(output_)-1] = 1
    return (input_ids, input_mask, output_ids, output_mask, token_ids, token_mask)

def get_rerank_scores(input_list, output_list):
    if input_list:
        longest_input = max(input_list, key=len)
        max_length_input = len(longest_input)
    else:
        max_length_input = 0
    if output_list:
        longest_output = max(output_list, key=len)
        max_length_output = len(longest_output)
    else:
        max_length_output = 0
    max_length_reward = max(max_length_input, max_length_output) + 2
    (input_ids, 
    input_mask, 
    output_ids, 
    output_mask, 
    token_ids,
    token_mask) = convert_symbols_to_inputs_reward(input_list, output_list, max_length_reward)

    input_ids = torch.LongTensor(input_ids).to(device)
    input_mask = torch.FloatTensor(input_mask).to(device)
    output_ids = torch.LongTensor(output_ids).to(device)
    output_mask = torch.FloatTensor(output_mask).to(device)
    token_ids = torch.LongTensor(token_ids).to(device)
    token_mask = torch.FloatTensor(token_mask).to(device)
    mutual_mask = get_mutual_mask_reward([output_mask, input_mask])
    input_mask = get_input_mask_reward(input_mask)
    output_mask = get_output_mask_reward(output_mask)
    logits = reward_model(input_ids, output_ids, input_mask, output_mask, mutual_mask)
    per_token_logps = torch.gather(logits.log_softmax(-1), dim=2, index=token_ids.unsqueeze(2)).squeeze(2)

    all_logps = (per_token_logps * token_mask).sum(-1) / token_mask.sum(-1)
    return all_logps

def check_reactant_is_material(reactant):
    return Chem.MolToInchiKey(Chem.MolFromSmiles(reactant))[:14] in stock_inchikeys


def check_reactants_are_material(reactants):
    for reactant in reactants:
        if Chem.MolToInchiKey(Chem.MolFromSmiles(reactant))[:14] not in stock_inchikeys:
            return False
    return True


def get_route_result(task):
    max_depth = task["depth"]
    # Initialization
    answer_set = []
    queue = []
    queue.append({
        "score": value_fn(task["product"]),
        "routes_info": [{"route": [task["product"]], "depth": 0}],  # List of routes information
        "starting_materials": [],
    })
    while True:
        if len(queue) == 0:
            break
        nxt_queue = []
        for item in queue:
            score = item["score"]
            routes_info = item["routes_info"]
            starting_materials = item["starting_materials"]
            first_route_info = routes_info[0]
            first_route, depth = first_route_info["route"], first_route_info["depth"]
            if depth > max_depth:
                continue
            expansion_mol = first_route[-1]
            for expansion_solution in get_beam(first_route[-1], args.beam_size):
                iter_routes = deepcopy(routes_info)
                iter_routes.pop(0)
                iter_starting_materials = deepcopy(starting_materials)
                expansion_reactants, reaction_cost = expansion_solution[0], expansion_solution[1]
                expansion_reactants = sorted(expansion_reactants)
                if check_reactants_are_material(expansion_reactants) and len(iter_routes) == 0:
                    answer_set.append({
                        "score": score+reaction_cost-value_fn(expansion_mol),
                        "starting_materials": iter_starting_materials+expansion_reactants,
                        })
                else:
                    estimation_cost = 0
                    for reactant in expansion_reactants:
                        if check_reactant_is_material(reactant):
                            iter_starting_materials.append(reactant)
                        else:
                            estimation_cost += value_fn(reactant)
                            iter_routes = [{"route": first_route+[reactant], "depth": depth+1}] + iter_routes
                    nxt_queue.append({
                        "score": score+reaction_cost+estimation_cost-value_fn(expansion_mol),
                        "routes_info": iter_routes,
                        "starting_materials": iter_starting_materials
                    })
        queue = sorted(nxt_queue, key=lambda x: x["score"])[:args.beam_size]
            
    answer_set = sorted(answer_set, key=lambda x: x["score"])
    record_answers = set()
    final_answer_set = []
    rerank_input_list = []
    rerank_output_list = []
    for item in answer_set:
        score = item["score"]
        starting_materials = item["starting_materials"]

        cano_starting_materials = []
        for material_ in starting_materials:
            _, cano_material_ = cano_smiles(material_)
            cano_starting_materials.append(cano_material_)

        answer_keys = [Chem.MolToInchiKey(Chem.MolFromSmiles(m))[:14] for m in starting_materials]
        if '.'.join(sorted(answer_keys)) not in record_answers:
            record_answers.add('.'.join(sorted(answer_keys)))
            final_answer_set.append({
                "score": score,
                "answer_keys": answer_keys
            })
            rerank_input_list.append(task['product'])
            rerank_output_list.append('.'.join(sorted(cano_starting_materials)))

    rerank_scores = get_rerank_scores(rerank_input_list, rerank_output_list)
    for i, score_ in enumerate(rerank_scores):
        final_answer_set[i]["rerank_score"] = -score_.item()
        final_answer_set[i]["total_score"] = -args.alpha*score_.item() + final_answer_set[i]["score"]
    final_answer_set = sorted(final_answer_set, key=lambda x: x["total_score"])[:args.beam_size]
    
    # Calculate answers
    ground_truth_keys_list = [
        set([
            Chem.MolToInchiKey(Chem.MolFromSmiles(target))[:14] for target in targets
        ]) for targets in task["targets"]
    ]
    for rank, answer in enumerate(final_answer_set):
        answer_keys = set(answer["answer_keys"])
        for ground_truth_keys in ground_truth_keys_list:
            if ground_truth_keys == answer_keys:
                return max_depth, rank
    
    return max_depth, None


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--fp_dim', type=int, default=2048)
    parser.add_argument('--n_layers', type=int, default=1)
    parser.add_argument('--latent_dim', type=int, default=128)
    parser.add_argument('--seed', type=int, default=42, help='Random seed.')
    parser.add_argument('--max_length', type=int, default=200, help='The max length of a molecule.')
    parser.add_argument('--embedding_size', type=int, default=64, help='The size of embeddings')
    parser.add_argument('--hidden_size', type=int, default=640, help='The size of hidden units')
    parser.add_argument('--num_hidden_layers', type=int, default=3, help='Number of layers in encoder\'s module. Default 3.')
    parser.add_argument('--num_attention_heads', type=int, default=10, help='Number of attention heads. Default 10.')
    parser.add_argument('--intermediate_size', type=int, default=512, help='The size of hidden units of position-wise layer.')
    parser.add_argument('--hidden_dropout_prob', type=float, default=0.1, help='Dropout rate (1 - keep probability).')
    parser.add_argument('--temperature', type=float, default=1.2, help='Temperature for decoding. Default 1.2')
    parser.add_argument('--beam_size', type=int, default=5, help='Beams size. Default 5. Must be 1 meaning greedy search or greater or equal 5.')
    parser.add_argument("--alpha", type=float, default=0.01)

    args = parser.parse_args()
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

    torch.manual_seed(args.seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed(args.seed)
    np.random.seed(args.seed)
    random.seed(args.seed)

    value_model = ValueMLP(
            n_layers=args.n_layers,
            fp_dim=args.fp_dim,
            latent_dim=args.latent_dim,
            dropout_rate=0.1
        )
    value_model.load_state_dict(torch.load('value_mlp.pkl'))
    value_model.to(device)
    value_model.eval()

    config = TransformerConfig(vocab_size=get_vocab_size(),
                           embedding_size=args.embedding_size,
                           hidden_size=args.hidden_size,
                           num_hidden_layers=args.num_hidden_layers,
                           num_attention_heads=args.num_attention_heads,
                           intermediate_size=args.intermediate_size,
                           hidden_dropout_prob=args.hidden_dropout_prob)
    predict_model = Transformer(config)
    checkpoint = torch.load("models/model.pkl")
    if isinstance(checkpoint, torch.nn.DataParallel):
        checkpoint = checkpoint.module
    predict_model.load_state_dict(checkpoint.state_dict())
    predict_model.to(device)
    predict_model.eval()

    char_to_ix = get_char_to_ix()
    ix_to_char = get_ix_to_char()
    vocab_size = get_vocab_size()

    stock = pd.read_hdf('zinc_stock_17_04_20.hdf5', key="table")  
    stockinchikey_list = stock.inchi_key.values
    stock_inchikeys = set([x[:14] for x in stockinchikey_list])

    reward_config = RewardTransformerConfig(vocab_size=vocab_size,
                                            embedding_size=64,
                                            hidden_size=512,
                                            num_hidden_layers=6,
                                            num_attention_heads=8,
                                            intermediate_size=1024,
                                            hidden_dropout_prob=0.1)
    reward_model = RewardTransformer(reward_config)
    checkpoint = torch.load("reward_model.pkl")
    reward_model.load_state_dict(checkpoint.state_dict())
    reward_model.to(device)
    reward_model.eval()

    tasks = load_dataset("test")
    overall_result = np.zeros((args.beam_size, 2))
    depth_hit = np.zeros((2, 15, args.beam_size))
    
    tasks = load_dataset('test')
    overall_result = np.zeros((args.beam_size, 2))
    depth_hit = np.zeros((2, 15, args.beam_size))
    for epoch in trange(0, len(tasks)):
        max_depth, rank = get_route_result(tasks[epoch])
        overall_result[:, 1] += 1
        depth_hit[1, max_depth, :] += 1
        if rank is not None:
            overall_result[rank:, 0] += 1
            depth_hit[0, max_depth, rank:] += 1

    print("overall_result: ", overall_result, 100 * overall_result[:, 0] / overall_result[:, 1])
    print("depth_hit: ", depth_hit, 100 * depth_hit[0, :, :] / depth_hit[1, :, :])
