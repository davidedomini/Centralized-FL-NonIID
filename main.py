import copy
import torch
from torch import nn
from torchvision import datasets, transforms
import torch.nn.functional as F
import numpy as np
from torch.utils.data import DataLoader, Dataset, random_split

class CNNMnist(nn.Module):

    def __init__(self):
        super(CNNMnist, self).__init__()
        self.conv1 = nn.Conv2d(1, 5, kernel_size=5)
        self.conv2 = nn.Conv2d(5, 10, kernel_size=5)
        self.conv2_drop = nn.Dropout2d()
        self.fc1 = nn.Linear(160, 20)
        self.fc2 = nn.Linear(20, 10)

    def forward(self, x):
        x = F.relu(F.max_pool2d(self.conv1(x), 2))
        x = F.relu(F.max_pool2d(self.conv2_drop(self.conv2(x)), 2))
        #print(x.shape[1], x.shape[2], x.shape[3])
        x = x.view(-1, x.shape[1] * x.shape[2] * x.shape[3])
        x = F.relu(self.fc1(x))
        x = F.dropout(x, training=self.training)
        x = self.fc2(x)
        return F.log_softmax(x, dim=1)

class DatasetSplit(Dataset):
    """An abstract Dataset class wrapped around Pytorch Dataset class.
    """

    def __init__(self, dataset, idxs):
        self.dataset = dataset
        self.idxs = [int(i) for i in idxs]

    def __len__(self):
        return len(self.idxs)

    def __getitem__(self, item):
        image, label = self.dataset[self.idxs[item]]
        return torch.tensor(image), torch.tensor(label)

def average_weights(models):
    """ Averages the weights

    Args:
        models (list): a list of state_dict

    Returns:
        state_dict: the average state_dict
    """
    w_avg = copy.deepcopy(models[0])
    for key in w_avg.keys():
        for i in range(1, len(models)):
            w_avg[key] += models[i][key]
        w_avg[key] = torch.div(w_avg[key], len(models))
    return w_avg


def get_test_dataset():
    apply_transform = transforms.ToTensor()

    dataset = datasets.MNIST('data',
                                   train=False,
                                   download=True,
                                   transform=apply_transform)

    return dataset

def get_dataset(indexes):

    apply_transform = transforms.ToTensor()

    train_dataset = datasets.MNIST('data',
                                   train=True,
                                   download=True,
                                   transform=apply_transform)

    dataset = DatasetSplit(train_dataset, indexes)

    return dataset


def dataset_to_nodes_partitioning(nodes_count: int, areas: int, random_seed: int, shuffling: bool = False, data_fraction = 1.0):
    np.random.seed(random_seed)  # set seed from Alchemist to make the partitioning deterministic
    apply_transform = transforms.ToTensor()

    train_dataset = datasets.MNIST('data', train=True, download=True, transform=apply_transform)

    nodes_per_area = int(nodes_count / areas)
    dataset_labels_count = len(train_dataset.classes)
    split_nodes_per_area = np.array_split(np.arange(nodes_count), areas)
    split_classes_per_area = np.array_split(np.arange(dataset_labels_count), areas)
    nodes_and_classes = zip(split_nodes_per_area, split_classes_per_area)

    index_mapping = {}

    for index, (nodes, classes) in enumerate(nodes_and_classes):
        records_per_class = [index for index, (_, lab) in enumerate(train_dataset) if lab in classes]
        # intra-class shuffling
        if shuffling:
            np.random.shuffle(records_per_class)
        split_record_per_node = np.array_split(records_per_class, nodes_per_area)
        for node in nodes:
            list = split_record_per_node[node % nodes_per_area].tolist()
            bound = int(len(list) * data_fraction)
            indexes = np.random.choice(len(list), bound)
            index_mapping[node] = (np.array(list)[indexes].tolist(), classes.tolist())

    return index_mapping

def local_training(model, epochs, data, batch_size):
    criterion = nn.NLLLoss()
    model.train()
    epoch_loss = []
    optimizer = torch.optim.Adam(model.parameters(), lr=0.001, weight_decay=1e-4)
    data_loader = DataLoader(data, batch_size=batch_size, shuffle=True)
    for _ in range(epochs):
        batch_loss = []
        for batch_index, (images, labels) in enumerate(data_loader):
            model.zero_grad()
            log_probs = model(images)
            loss = criterion(log_probs, labels)
            loss.backward()
            optimizer.step()
            batch_loss.append(loss.item())
        mean_epoch_loss = sum(batch_loss) / len(batch_loss)
        epoch_loss.append(mean_epoch_loss)
    return model.state_dict(), sum(epoch_loss) / len(epoch_loss)

def evaluate(model_w, data, batch_size):
    model = CNNMnist()
    model.load_state_dict(model_w)
    criterion = nn.NLLLoss()
    model.eval()
    loss, total, correct = 0.0, 0.0, 0.0
    data_loader = DataLoader(data, batch_size=batch_size, shuffle=False)
    for batch_index, (images, labels) in enumerate(data_loader):
        outputs = model(images)
        batch_loss = criterion(outputs, labels)
        loss += batch_loss.item()

        _, pred_labels = torch.max(outputs, 1)
        pred_labels = pred_labels.view(-1)
        correct += torch.sum(torch.eq(pred_labels, labels)).item()
        total += len(labels)

    accuracy = correct / total
    return accuracy, loss
    
def train_val_split(data):
    train_size = int(len(data) * 0.8)
    validation_size = len(data) - train_size
    train_set, val_set = random_split(data, [train_size, validation_size])
    return train_set, val_set


if __name__ == '__main__':
    torch.manual_seed(1)
    devices = 20
    areas = 4
    #dataset_to_nodes_partitioning(nodes_count: int, areas: int, random_seed: int, shuffling: bool = False, data_fraction = 1.0):
    mapping = dataset_to_nodes_partitioning(devices, areas, 1, True, data_fraction=0.2)
    global_rounds = 40
    local_epochs = 2
    batch_size = 64

    global_model = CNNMnist()
    global_weights = global_model.state_dict()
    
    train_loss, train_accuracy = [], []
    val_acc_list, net_list = [], []
    cv_loss, cv_accuracy = [], []
    val_loss_pre, counter = 0, 0

    for round in range(global_rounds):
        local_weights, local_losses = [], []
        val_losses, val_accuracies = [], []
        print(f'\n | Global training round: {round+1} | \n')
        global_model.train()
        
        for i in range(devices): #for each device
            idxs, _ = mapping[i]
            print(i, len(idxs))
            data = get_dataset(idxs) 
            train_data, validation_data = train_val_split(data)
            local_model, train_loss = local_training(copy.deepcopy(global_model), local_epochs, train_data, batch_size)
            val_accuracy, val_loss = evaluate(local_model, validation_data, batch_size)
            local_weights.append(copy.deepcopy(local_model))
            local_losses.append(copy.deepcopy(train_loss))
            val_losses.append(copy.deepcopy(val_loss))
            val_accuracies.append(copy.deepcopy(val_accuracy))
        
        global_weights = average_weights(local_weights)
        global_model.load_state_dict(global_weights)

        mean_train_loss = sum(local_losses) / len(local_losses)
        mean_val_loss = sum(val_losses) / len(val_losses)
        mean_val_accuracy = sum(val_accuracies) / len(val_accuracies)

        print(f'Mean train loss: {mean_train_loss}')
        print(f'Mean val loss: {mean_val_loss}')
        print(f'Mean val accuracy: {mean_val_accuracy}')

    test_dataset = get_test_dataset()
    test_acc, test_loss = evaluate(global_model.state_dict(), test_dataset, 64)
    print(f'\n | Testing | \n')
    print(f'Accuracy: {test_acc}')